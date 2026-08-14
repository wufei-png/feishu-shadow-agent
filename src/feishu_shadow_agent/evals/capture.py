from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from ..config import LoadedConfig
from ..ingestion import MessageNormalizer, normalize_message_sent_at
from ..paths import resolve_relative_path
from ..time_utils import format_instant, parse_instant_or_none, utc_now
from ..types import LarkCliResult, MessagePage, NormalizedMessage
from .artifacts import (
    EvalError,
    copy_config_or_raise,
    evals_base_dir,
    file_sha256,
    message_id_from_raw,
    read_yaml,
    reserve_run_dir,
    text_excerpt,
    validate_config_copy,
    write_jsonl,
    write_metadata,
    write_yaml,
)
from .cases import message_sent_at, resource_fixture_path
from .schemas import ResourceFixture


class CaptureLarkClient(Protocol):
    get_messages: Callable[..., MessagePage]
    search_messages: Callable[..., MessagePage]
    list_chat_messages: Callable[..., MessagePage]
    version: Callable[..., LarkCliResult]
    download_resource: Callable[..., LarkCliResult]


class CaptureService:
    def __init__(self, *, loaded: LoadedConfig, lark_client: CaptureLarkClient) -> None:
        self.loaded = loaded
        self.lark_client = lark_client
        self.normalizer = MessageNormalizer(owner_open_id=loaded.config.owner.open_id)

    def candidates(self, *, lookback_days: int, limit: int) -> list[dict[str, Any]]:
        if lookback_days < 1 or limit < 1:
            raise EvalError("lookback-days and limit must be positive")
        start, end = _lookback_window(lookback_days)
        by_id: dict[str, dict[str, Any]] = {}
        for chat_type, is_at_me in (("group", True), ("p2p", False)):
            for raw in self._drain_search(
                chat_type=chat_type,
                is_at_me=is_at_me,
                start=start,
                end=end,
            ):
                message_id = message_id_from_raw(raw)
                if message_id:
                    source = "group_at_me" if chat_type == "group" else "p2p"
                    row = by_id.setdefault(
                        message_id,
                        _candidate_row(raw, default_chat_type=chat_type),
                    )
                    if source not in row["sources"]:
                        row["sources"].append(source)
        rows = sorted(
            by_id.values(), key=lambda row: str(row.get("sent_at") or ""), reverse=True
        )
        return rows[:limit]

    def capture(
        self,
        *,
        message_id: str,
        context_before: int,
        context_after: int,
        lookback_days: int,
        label: str | None,
        allow_sensitive_config: bool,
    ) -> Path:
        if context_before < 0 or context_after < 0:
            raise EvalError("context-before/context-after cannot be negative")
        if lookback_days < 1:
            raise EvalError("lookback-days must be positive")
        validate_config_copy(
            loaded=self.loaded,
            allow_sensitive_config=allow_sensitive_config,
        )
        seed_page = self.lark_client.get_messages(
            as_identity="user", message_ids=[message_id]
        )
        if not seed_page.items:
            raise EvalError(f"message not found: {message_id}")
        seed = seed_page.items[0]
        message_sent_at(seed)
        context = self._capture_context(
            seed,
            context_before=context_before,
            context_after=context_after,
            lookback_days=lookback_days,
        )
        task_fixtures, task_raws = self._task_fixtures(seed=seed, context=context)
        if any(not message_id_from_raw(raw) for raw in [*context, *task_raws]):
            raise EvalError("captured context contains a message without message_id")
        raw_by_id = {message_id_from_raw(raw): raw for raw in [*context, *task_raws]}
        raw_by_id[message_id] = seed
        raws = sorted(
            raw_by_id.values(),
            key=lambda raw: (_raw_sent_at(raw) or "", message_id_from_raw(raw)),
        )
        _, case_dir = reserve_run_dir(
            evals_base_dir(self.loaded) / "captured", "capture", label
        )
        write_jsonl(case_dir / "messages.jsonl", raws)
        resource_fixtures, resource_errors = self._capture_resources(
            case_dir=case_dir, raws=raws
        )
        config_info = copy_config_or_raise(
            loaded=self.loaded,
            destination_dir=case_dir,
            allow_sensitive_config=allow_sensitive_config,
        )
        write_metadata(
            case_dir,
            loaded=self.loaded,
            config_info=config_info,
            lark_cli_version=self._lark_cli_version(),
        )
        if resource_errors:
            metadata = read_yaml(case_dir / "metadata.yaml")
            metadata["resource_capture_errors"] = resource_errors
            write_yaml(case_dir / "metadata.yaml", metadata)
        source = self._resolve_source(seed)
        target_resources = [
            fixture.model_dump(mode="json")
            for fixture in resource_fixtures
            if fixture.message_id == message_id
        ]
        write_yaml(
            case_dir / "router.review.yaml",
            {
                "schema_version": "router_review_v1",
                "scenario": {
                    "case_type": "router",
                    "target": {"message_id": message_id, "source": source},
                    "tasks": task_fixtures,
                },
                "labels": {
                    "route": None,
                    "task_key": None,
                },
            },
        )
        write_yaml(
            case_dir / "task_session.review.yaml",
            {
                "schema_version": "task_session_review_v1",
                "scenario": {
                    "case_type": "task-session",
                    "mode": "initial",
                    "message_ids": [message_id],
                    "resources": target_resources,
                },
                "labels": {
                    "reference_answer": None,
                    "answerability": None,
                    "decision_reason": None,
                    "watch_action": None,
                    "expected_skills": [],
                },
            },
        )
        write_yaml(
            case_dir / "full_chain.review.yaml",
            {
                "schema_version": "full_chain_review_v1",
                "scenario": {
                    "case_type": "full-chain",
                    "setup": [],
                    "target": {"message_id": message_id, "source": source},
                    "resources": target_resources,
                },
                "labels": {
                    "router": {"route": None, "task_key": None},
                    "task_session": None,
                    "reference_answer": None,
                },
            },
        )
        _write_review_markdown(case_dir, resource_errors=resource_errors)
        return case_dir

    def _resolve_source(self, seed: dict[str, Any]) -> str:
        message = self.normalizer.normalize(seed)
        if message.chat_type is not None:
            return _infer_source(message)
        sent_at = _parse_datetime(_raw_sent_at(seed))
        if not message.chat_id or sent_at is None:
            return _infer_source(message)
        page = self.lark_client.search_messages(
            chat_id=message.chat_id,
            chat_type=None,
            is_at_me=False,
            start=(sent_at - timedelta(minutes=1)).isoformat(),
            end=(sent_at + timedelta(minutes=1)).isoformat(),
        )
        for raw in page.items:
            if message_id_from_raw(raw) != message.message_id:
                continue
            chat_type = raw.get("chat_type") or raw.get("chatType")
            return _infer_source(
                self.normalizer.normalize(seed, default_chat_type=chat_type)
            )
        return _infer_source(message)

    def _capture_context(
        self,
        seed: dict[str, Any],
        *,
        context_before: int,
        context_after: int,
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        chat_id = _raw_chat_id(seed)
        if not chat_id:
            return [seed]
        sent_at = _raw_sent_at(seed)
        if sent_at is None:
            return [seed]
        parsed = _parse_datetime(sent_at)
        if parsed is None:
            return [seed]
        before = self._capture_direction(
            chat_id=chat_id,
            start=(parsed - timedelta(days=lookback_days)).isoformat(),
            end=sent_at,
            order="desc",
            limit=context_before,
            seed_id=message_id_from_raw(seed),
        )
        after = self._capture_direction(
            chat_id=chat_id,
            start=sent_at,
            end=(parsed + timedelta(days=lookback_days)).isoformat(),
            order="asc",
            limit=context_after,
            seed_id=message_id_from_raw(seed),
        )
        rows = [*before, seed, *after]
        by_id = {message_id_from_raw(row): row for row in rows}
        return sorted(
            by_id.values(),
            key=lambda raw: (_raw_sent_at(raw) or "", message_id_from_raw(raw)),
        )

    def _task_fixtures(
        self, *, seed: dict[str, Any], context: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        database = resolve_relative_path(
            self.loaded.config.storage.sqlite_path, self.loaded.base_dir
        )
        if not database.is_file():
            return {}, []
        seed_message = self.normalizer.normalize(seed)
        if not seed_message.chat_id:
            return {}, []
        selected_ids = {
            message_id_from_raw(raw) for raw in context if message_id_from_raw(raw)
        }
        target_time = _raw_sent_at(seed) or ""
        watch_keys = _message_watch_keys(seed_message)
        if seed_message.direct_mention:
            active_condition = (
                "t.status = 'watching' AND "
                "(t.watch_until IS NULL OR julianday(t.watch_until) > julianday(?))"
            )
        elif watch_keys:
            watch_placeholders = ",".join("?" for _ in watch_keys)
            active_condition = (
                # Only generated `?` placeholders are interpolated; all values
                # remain SQLite parameters.
                "t.status = 'watching' "  # noqa: S608
                "AND (t.watch_until IS NULL OR "
                "julianday(t.watch_until) > julianday(?)) "
                "AND EXISTS ("
                "SELECT 1 FROM task_watch_keys wk "
                "WHERE wk.task_id = t.id "
                f"AND wk.key IN ({watch_placeholders})"
                ")"
            )
        else:
            active_condition = "0"
        linked_condition = ""
        params: list[Any] = [seed_message.chat_id]
        if active_condition != "0":
            params.append(target_time)
            if not seed_message.direct_mention:
                params.extend(watch_keys)
        if selected_ids:
            placeholders = ",".join("?" for _ in selected_ids)
            linked_condition = f" OR tm.message_id IN ({placeholders})"
            params.extend(sorted(selected_ids))
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            task_rows = connection.execute(
                # The condition fragments contain only fixed SQL and generated
                # placeholders; user values remain parameterized below.
                f"""
                SELECT DISTINCT t.id, t.status, t.task_label
                FROM tasks t
                LEFT JOIN task_messages tm ON tm.task_id = t.id
                WHERE t.chat_id = ?
                  AND (
                    ({active_condition})
                    {linked_condition}
                  )
                ORDER BY t.id
                """,  # noqa: S608
                params,
            ).fetchall()
            target_route = connection.execute(
                """
                SELECT route, target_task_id
                FROM routing_audits
                WHERE message_id = ?
                  AND route != 'ignore'
                ORDER BY id DESC
                LIMIT 1
                """,
                (message_id_from_raw(seed),),
            ).fetchone()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise EvalError(f"failed to read capture task fixtures: {exc}") from exc
        fixtures: dict[str, Any] = {}
        raw_messages: list[dict[str, Any]] = []
        target_pre_status: dict[int, str] = {}
        if target_route is not None and target_route["target_task_id"] is not None:
            route = str(target_route["route"])
            if route in {"attach_task", "human_taken_over"}:
                target_pre_status[int(target_route["target_task_id"])] = "watching"
            elif route == "reopen_task":
                target_pre_status[int(target_route["target_task_id"])] = "closed"
        try:
            for task_row in task_rows:
                messages = connection.execute(
                    """
                    SELECT m.message_id, m.sent_at, m.raw_json
                    FROM task_messages tm
                    JOIN messages m ON m.message_id = tm.message_id
                    WHERE tm.task_id = ? AND (
                        m.sent_at IS NULL OR julianday(m.sent_at) < julianday(?)
                    )
                    ORDER BY julianday(m.sent_at), m.message_id
                    """,
                    (task_row["id"], target_time),
                ).fetchall()
                message_ids: list[str] = []
                task_raw_messages: list[dict[str, Any]] = []
                for row in messages:
                    try:
                        raw = json.loads(row["raw_json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(raw, dict):
                        continue
                    raw = cast(dict[str, Any], raw)
                    if message_id_from_raw(raw) != str(row["message_id"]):
                        continue
                    message_ids.append(str(row["message_id"]))
                    task_raw_messages.append(raw)
                if not message_ids:
                    continue
                status = target_pre_status.get(int(task_row["id"]), task_row["status"])
                if status == "watching" and _fixture_expired_at_target(
                    task_raw_messages,
                    target_time=target_time,
                    watch_minutes=self.loaded.config.lifecycle.watch_minutes,
                ):
                    continue
                raw_messages.extend(task_raw_messages)
                alias = f"task_{len(fixtures) + 1}"
                fixtures[alias] = {
                    "status": status,
                    "task_label": task_row["task_label"],
                    "message_ids": message_ids,
                }
        finally:
            connection.close()
        return fixtures, raw_messages

    def _capture_resources(
        self, *, case_dir: Path, raws: list[dict[str, Any]]
    ) -> tuple[list[ResourceFixture], list[dict[str, str]]]:
        fixtures: list[ResourceFixture] = []
        errors: list[dict[str, str]] = []
        for raw in raws:
            message = self.normalizer.normalize(raw)
            for resource in message.resources:
                provisional = ResourceFixture(
                    message_id=resource.message_id,
                    file_key=resource.file_key,
                    resource_type=resource.resource_type,
                    sha256="0" * 64,
                )
                destination = resource_fixture_path(case_dir, provisional)
                destination.parent.mkdir(parents=True, exist_ok=True)
                relative_destination = destination.relative_to(self.loaded.base_dir)
                try:
                    result = self.lark_client.download_resource(
                        message_id=resource.message_id,
                        file_key=resource.file_key,
                        resource_type=resource.resource_type,
                        output=relative_destination.as_posix(),
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "message_id": resource.message_id,
                            "file_key": resource.file_key,
                            "error": str(exc),
                        }
                    )
                    continue
                if not result.ok or not destination.is_file():
                    errors.append(
                        {
                            "message_id": resource.message_id,
                            "file_key": resource.file_key,
                            "error": result.error or result.stderr or "download failed",
                        }
                    )
                    continue
                fixtures.append(
                    provisional.model_copy(update={"sha256": file_sha256(destination)})
                )
        return fixtures, errors

    def _capture_direction(
        self,
        *,
        chat_id: str,
        start: str,
        end: str,
        order: str,
        limit: int,
        seed_id: str,
    ) -> list[dict[str, Any]]:
        if limit == 0:
            return []
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_tokens: set[str] = set()
        token: str | None = None
        while len(rows) < limit:
            page = self.lark_client.list_chat_messages(
                chat_id=chat_id,
                start=start,
                end=end,
                page_token=token,
                page_size=min(max(limit + 1, 1), 50),
                order=order,
            )
            for row in page.items:
                message_id = message_id_from_raw(row)
                if not message_id or message_id == seed_id or message_id in seen:
                    continue
                seen.add(message_id)
                rows.append(row)
                if len(rows) == limit:
                    break
            if not page.has_more or not page.next_page_token:
                break
            if page.next_page_token in seen_tokens:
                raise EvalError(
                    f"capture context pagination token loop: {page.next_page_token}"
                )
            seen_tokens.add(page.next_page_token)
            token = page.next_page_token
        return rows

    def _drain_search(
        self,
        *,
        chat_type: str,
        is_at_me: bool,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        token: str | None = None
        while True:
            page = self.lark_client.search_messages(
                chat_type=chat_type,
                is_at_me=is_at_me,
                start=start,
                end=end,
                page_token=token,
            )
            rows.extend(page.items)
            if not page.has_more or not page.next_page_token:
                break
            if page.next_page_token in seen_tokens:
                raise EvalError(
                    f"capture search pagination token loop: {page.next_page_token}"
                )
            seen_tokens.add(page.next_page_token)
            token = page.next_page_token
        return rows

    def _lark_cli_version(self) -> str | None:
        try:
            result = self.lark_client.version()
        except Exception:  # noqa: BLE001
            return None
        return result.stdout.strip() if result.ok and result.stdout.strip() else None


def _write_review_markdown(
    directory: Path, *, resource_errors: list[dict[str, str]]
) -> None:
    resource_note = (
        "\nResource capture failed. Fix resources before running or promoting this case.\n"
        if resource_errors
        else ""
    )
    (directory / "REVIEW.md").write_text(
        """# Eval Review

Edit the type-specific `*.review.yaml` file directly. Draft scenarios are runnable;
unset labels produce diagnostics with `passed: null`. Promotion is the human
confirmation boundary and requires every golden field to be complete.

Use `eval promote --type <type> --case <this-dir> --review <review-file> --name <name>`.
"""
        + resource_note,
        encoding="utf-8",
    )


def _candidate_row(raw: dict[str, Any], *, default_chat_type: str) -> dict[str, Any]:
    return {
        "sent_at": _raw_sent_at(raw),
        "chat_type": raw.get("chat_type") or default_chat_type,
        "sender": _raw_sender_name(raw),
        "message_id": message_id_from_raw(raw),
        "text_excerpt": text_excerpt(_raw_text(raw)),
        "sources": [],
    }


def _infer_source(message: Any) -> str:
    if message.chat_type == "p2p":
        return "p2p"
    if message.direct_mention:
        return "group_at_me"
    return "active_watch"


def _lookback_window(days: int) -> tuple[str, str]:
    end = utc_now().replace(microsecond=0)
    return format_instant(end - timedelta(days=days)), format_instant(end)


def _parse_datetime(value: str | None) -> datetime | None:
    return parse_instant_or_none(value)


def _raw_chat_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("chat_id") or raw.get("chatId")
    if isinstance(value, str) and value:
        return value
    chat = raw.get("chat")
    if isinstance(chat, dict):
        chat_map = cast(dict[str, Any], chat)
        value = chat_map.get("chat_id") or chat_map.get("chatId") or chat_map.get("id")
        return value if isinstance(value, str) and value else None
    return None


def _raw_sent_at(raw: dict[str, Any]) -> str | None:
    for key in ("create_time", "created_at", "sent_at", "timestamp"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return normalize_message_sent_at(value)
    return None


def _raw_sender_name(raw: dict[str, Any]) -> str | None:
    for source in (raw, raw.get("sender")):
        if not isinstance(source, dict):
            continue
        source_map = cast(dict[str, Any], source)
        for key in ("sender_name", "senderName", "user_name", "userName", "name", "id"):
            value = source_map.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _raw_text(raw: dict[str, Any]) -> str:
    for key in ("text", "message"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    content = raw.get("content")
    if isinstance(content, dict):
        content_map = cast(dict[str, Any], content)
        return str(content_map.get("text") or content_map.get("title") or "")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(parsed, dict):
            parsed_map = cast(dict[str, Any], parsed)
            return str(parsed_map.get("text") or parsed_map.get("title") or "")
    return ""


def _message_watch_keys(message: NormalizedMessage) -> list[str]:
    keys: list[str] = []
    if message.reply_to_message_id:
        keys.append(f"msg:{message.reply_to_message_id}")
    if message.thread_id:
        keys.append(f"thread:{message.thread_id}")
    if message.sender_id:
        keys.append(f"user:{message.sender_id}")
    return keys


def _fixture_expired_at_target(
    raws: list[dict[str, Any]], *, target_time: str, watch_minutes: int
) -> bool:
    target = _parse_datetime(target_time)
    latest = _parse_datetime(_raw_sent_at(raws[-1])) if raws else None
    if target is None or latest is None:
        return True
    return latest + timedelta(minutes=watch_minutes) <= target
