from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..agent_backend import AgentBackend
from ..config import LoadedConfig
from ..feishu.lark_cli import LarkCliClient
from ..paths import resolve_relative_path
from ..time_utils import format_instant, parse_instant, utc_now, utc_now_iso
from .artifacts import (
    EvalError,
    copy_config_or_raise,
    evals_base_dir,
    file_sha256,
    message_id_from_raw,
    read_jsonl,
    read_yaml,
    reserve_run_dir,
    validate_config_copy,
    write_jsonl,
    write_metadata,
    write_yaml,
)
from .backend_trace import TracedAgentBackend
from .ingress import (
    GroupIngressEvaluator,
    active_watch_sources,
    build_review_labels,
    compare_ingress_golden,
    run_ingress_judge,
    timeline_messages,
)
from .schemas import EvalProvenance, IngressGoldenLabels, IngressScenario

BackendFactory = Callable[[LoadedConfig], AgentBackend]


class IngressEvalService:
    def __init__(
        self,
        *,
        loaded: LoadedConfig,
        lark_client: LarkCliClient,
        backend_factory: BackendFactory,
    ) -> None:
        self.loaded = loaded
        self.lark_client = lark_client
        self.backend_factory = backend_factory
        self.base_dir = evals_base_dir(loaded)

    def run(
        self,
        *,
        chat_id: str | None,
        snapshot: Path | None,
        start: str | None,
        end: str | None,
        lookback_days: int | None,
        label: str | None,
        dry_run_backend: bool,
        allow_sensitive_config: bool,
    ) -> Path:
        validate_config_copy(
            loaded=self.loaded,
            allow_sensitive_config=allow_sensitive_config,
        )
        if snapshot is None:
            if not chat_id:
                raise EvalError("live run-ingress requires --chat-id")
            effective_start, effective_end = _window(
                start=start, end=end, lookback_days=lookback_days
            )
            raws = self._drain_chat(
                chat_id=chat_id, start=effective_start, end=effective_end
            )
            raws = _dedupe_raws(raws)
            group_at_ids = {
                message_id_from_raw(raw)
                for raw in self._drain_group_at_me(
                    start=effective_start, end=effective_end
                )
                if _raw_chat_id(raw) == chat_id
            }
            active_tasks = _active_task_fixtures(
                loaded=self.loaded,
                chat_id=chat_id,
                now=utc_now_iso(),
            )
            scenario = IngressScenario.model_validate(
                {"acquisition": {"active_tasks": active_tasks}}
            )
            chat = {
                "chat_id": chat_id,
                "start": effective_start,
                "end": effective_end,
            }
            source_kind = "live"
            lark_version = self._lark_cli_version()
            case_config_hash = file_sha256(self.loaded.path)
        else:
            if any(value is not None for value in (chat_id, start, end, lookback_days)):
                raise EvalError(
                    "snapshot mode does not accept chat/window options; they come from the snapshot"
                )
            snapshot_dir = snapshot.expanduser().resolve()
            raws, scenario, chat, group_at_ids = _load_snapshot(
                snapshot_dir, run_config=self.loaded
            )
            source_kind = "snapshot"
            lark_version = None
            case_config_hash = file_sha256(snapshot_dir / "config.yaml")

        sources_by_message = _sources_by_message(
            raws=raws,
            owner_open_id=self.loaded.config.owner.open_id,
            group_at_ids=group_at_ids,
            scenario=scenario,
        )
        timeline = GroupIngressEvaluator(
            owner_open_id=self.loaded.config.owner.open_id
        ).build_timeline(
            raws,
            sources_by_message=sources_by_message,
            owner=self.loaded.config.owner.model_dump(mode="json"),
            chat=chat,
        )
        run_id, run_dir = reserve_run_dir(
            self.base_dir / "ingress-runs", "ingress", label
        )
        timeline["run_id"] = run_id
        write_jsonl(run_dir / "raw_messages.jsonl", raws)
        write_yaml(run_dir / "eval_case.yaml", scenario.model_dump(mode="json"))
        write_yaml(run_dir / "ingress_timeline.yaml", timeline)
        config_info = copy_config_or_raise(
            loaded=self.loaded,
            destination_dir=run_dir,
            allow_sensitive_config=allow_sensitive_config,
        )
        write_metadata(
            run_dir,
            loaded=self.loaded,
            config_info=config_info,
            lark_cli_version=lark_version,
        )
        judge_labels: dict[str, dict[str, str]] = {}
        judge_error: str | None = None
        if not dry_run_backend:
            traced_backend: TracedAgentBackend | None = None
            try:
                traced_backend = TracedAgentBackend(self.backend_factory(self.loaded))
                judge_labels, judge_error = run_ingress_judge(
                    backend=traced_backend,
                    timeline=timeline,
                    cwd=str(self.loaded.base_dir),
                )
            except Exception as exc:  # noqa: BLE001
                judge_error = f"judge setup raised {type(exc).__name__}: {exc}"
            if traced_backend is not None:
                metadata = read_yaml(run_dir / "metadata.yaml")
                metadata["prompt_hashes"] = traced_backend.prompt_hashes()
                write_yaml(run_dir / "metadata.yaml", metadata)
                if self.loaded.config.debug.save_full_agent_io:
                    traced_backend.write_prompts(run_dir / "prompts")
        labels = build_review_labels(timeline, judge_labels=judge_labels)
        write_yaml(run_dir / "labels.review.yaml", labels)
        write_yaml(
            run_dir / "run_report.yaml",
            {
                "schema_version": "ingress_run_report_v1",
                "source_kind": source_kind,
                "message_count": len(raws),
                "case_config_hash": case_config_hash,
                "run_config_hash": file_sha256(self.loaded.path),
                "config_changed": case_config_hash != file_sha256(self.loaded.path),
                "judge_status": "skipped"
                if dry_run_backend
                else "error"
                if judge_error
                else "completed",
                "judge_error": judge_error,
            },
        )
        _write_review_markdown(run_dir, judge_error=judge_error)
        return run_dir

    def run_golden(
        self,
        *,
        case_dir: Path,
        label: str | None,
        allow_sensitive_config: bool,
    ) -> tuple[Path, int]:
        validate_config_copy(
            loaded=self.loaded,
            allow_sensitive_config=allow_sensitive_config,
        )
        golden = case_dir.expanduser().resolve()
        raws, scenario, chat, group_at_ids = _load_snapshot(
            golden, run_config=self.loaded
        )
        try:
            labels = IngressGoldenLabels.model_validate(
                read_yaml(golden / "labels.yaml")
            )
            provenance = EvalProvenance.model_validate(
                read_yaml(golden / "provenance.yaml")
            )
        except ValidationError as exc:
            raise EvalError(f"invalid ingress golden artifact: {exc}") from exc
        if provenance.source.kind != "ingress_run":
            raise EvalError("ingress golden provenance source must be ingress_run")
        _aware_datetime(provenance.promoted_at)
        timeline = GroupIngressEvaluator(
            owner_open_id=self.loaded.config.owner.open_id
        ).build_timeline(
            raws,
            sources_by_message=_sources_by_message(
                raws=raws,
                owner_open_id=self.loaded.config.owner.open_id,
                group_at_ids=group_at_ids,
                scenario=scenario,
            ),
            owner=self.loaded.config.owner.model_dump(mode="json"),
            chat=chat,
        )
        comparison = compare_ingress_golden(
            timeline=timeline, labels=labels.model_dump(mode="json")
        )
        _, run_dir = reserve_run_dir(
            self.base_dir / "runs" / "ingress", "ingress-golden", label
        )
        config_info = copy_config_or_raise(
            loaded=self.loaded,
            destination_dir=run_dir,
            allow_sensitive_config=allow_sensitive_config,
        )
        write_metadata(run_dir, loaded=self.loaded, config_info=config_info)
        report = {
            "schema_version": "ingress_golden_report_v1",
            "golden_case": str(golden),
            "case_config_hash": file_sha256(golden / "config.yaml"),
            "run_config_hash": file_sha256(self.loaded.path),
            "config_changed": file_sha256(golden / "config.yaml")
            != file_sha256(self.loaded.path),
            **comparison,
        }
        write_yaml(run_dir / "report.yaml", report)
        return run_dir, 0 if report["summary"]["passed"] else 1

    def _drain_chat(
        self, *, chat_id: str, start: str, end: str
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            page = self.lark_client.list_chat_messages(
                chat_id=chat_id,
                start=start,
                end=end,
                page_token=token,
            )
            rows.extend(page.items)
            if not page.has_more or not page.next_page_token:
                return rows
            if page.next_page_token in seen_tokens:
                raise EvalError(
                    f"ingress chat pagination token loop: {page.next_page_token}"
                )
            seen_tokens.add(page.next_page_token)
            token = page.next_page_token

    def _drain_group_at_me(self, *, start: str, end: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            page = self.lark_client.search_messages(
                chat_type="group",
                is_at_me=True,
                start=start,
                end=end,
                page_token=token,
            )
            rows.extend(page.items)
            if not page.has_more or not page.next_page_token:
                return rows
            if page.next_page_token in seen_tokens:
                raise EvalError(
                    f"ingress search pagination token loop: {page.next_page_token}"
                )
            seen_tokens.add(page.next_page_token)
            token = page.next_page_token

    def _lark_cli_version(self) -> str | None:
        try:
            result = self.lark_client.version()
        except Exception:  # noqa: BLE001
            return None
        return result.stdout.strip() if result.ok and result.stdout.strip() else None


def _load_snapshot(
    directory: Path, *, run_config: LoadedConfig
) -> tuple[list[dict[str, Any]], IngressScenario, dict[str, Any], set[str]]:
    if not directory.is_dir():
        raise EvalError(f"ingress snapshot directory does not exist: {directory}")
    baseline = read_yaml(directory / "config.yaml")
    owner = baseline.get("owner")
    owner_open_id = owner.get("open_id") if isinstance(owner, dict) else None
    if owner_open_id != run_config.config.owner.open_id:
        raise EvalError(
            "snapshot config owner.open_id must match Evaluation Run Config owner.open_id"
        )
    rows = read_jsonl(directory / "raw_messages.jsonl")
    if any(not isinstance(row, dict) for row in rows):
        raise EvalError("raw_messages.jsonl must contain only objects")
    try:
        scenario = IngressScenario.model_validate(
            read_yaml(directory / "eval_case.yaml")
        )
    except ValidationError as exc:
        raise EvalError(f"invalid ingress acquisition scenario: {exc}") from exc
    timeline = read_yaml(directory / "ingress_timeline.yaml")
    chat = timeline.get("chat")
    if not isinstance(chat, dict):
        raise EvalError("ingress snapshot timeline is missing chat metadata")
    timeline_rows = timeline_messages(timeline)
    timeline_ids = [str(row.get("message_id") or "") for row in timeline_rows]
    raw_ids = [message_id_from_raw(row) for row in rows]
    if not all(timeline_ids) or len(timeline_ids) != len(set(timeline_ids)):
        raise EvalError("snapshot timeline contains missing or duplicate message ids")
    if not all(raw_ids) or len(raw_ids) != len(set(raw_ids)):
        raise EvalError(
            "snapshot raw messages contain missing or duplicate message ids"
        )
    if set(timeline_ids) != set(raw_ids):
        raise EvalError("snapshot timeline must exactly cover raw_messages.jsonl")
    for row in timeline_rows:
        sources = row.get("sources")
        if not isinstance(sources, list) or any(
            source not in {"group_at_me", "active_watch"} for source in sources
        ):
            raise EvalError("snapshot timeline contains invalid acquisition sources")
    group_at_ids = {
        str(row["message_id"])
        for row in timeline_rows
        if "group_at_me" in (row.get("sources") or [])
    }
    return list(rows), scenario, chat, group_at_ids


def _sources_by_message(
    *,
    raws: list[dict[str, Any]],
    owner_open_id: str,
    group_at_ids: set[str],
    scenario: IngressScenario,
) -> dict[str, set[str]]:
    active_ids = active_watch_sources(
        raws,
        owner_open_id=owner_open_id,
        active_tasks=scenario.acquisition.active_tasks,
    )
    sources: dict[str, set[str]] = {}
    for raw in raws:
        message_id = message_id_from_raw(raw)
        values: set[str] = set()
        if message_id in group_at_ids:
            values.add("group_at_me")
        if message_id in active_ids:
            values.add("active_watch")
        sources[message_id] = values
    return sources


def _active_task_fixtures(
    *, loaded: LoadedConfig, chat_id: str, now: str
) -> dict[str, dict[str, Any]]:
    database = resolve_relative_path(loaded.config.storage.sqlite_path, loaded.base_dir)
    if not database.is_file():
        return {}
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT t.id, t.chat_id, t.thread_id, wk.key
            FROM tasks t
            JOIN task_watch_keys wk ON wk.task_id = t.id
            WHERE t.status = 'watching'
              AND (t.watch_until IS NULL OR julianday(t.watch_until) > julianday(?))
              AND t.chat_id = ?
            ORDER BY t.id, wk.key
            """,
            (now, chat_id),
        ).fetchall()
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise EvalError(f"failed to read active-watch fixtures: {exc}") from exc
    fixtures: dict[int, dict[str, Any]] = {}
    try:
        for row in rows:
            fixture = fixtures.setdefault(
                int(row["id"]),
                {
                    "chat_id": row["chat_id"],
                    "thread_id": row["thread_id"],
                    "watch_keys": [],
                },
            )
            fixture["watch_keys"].append(str(row["key"]))
    finally:
        if connection is not None:
            connection.close()
    return {
        f"task_{index}": fixture
        for index, fixture in enumerate(fixtures.values(), start=1)
    }


def _window(
    *, start: str | None, end: str | None, lookback_days: int | None
) -> tuple[str, str]:
    if start is not None or end is not None:
        if not start or not end:
            raise EvalError("--start and --end must be provided together")
        _aware_datetime(start)
        _aware_datetime(end)
        if _aware_datetime(start) >= _aware_datetime(end):
            raise EvalError("--start must be earlier than --end")
        return start, end
    if lookback_days is None or lookback_days < 1:
        raise EvalError("live run-ingress requires --lookback-days or --start/--end")
    end_dt = utc_now()
    start_dt = end_dt - timedelta(days=lookback_days)
    return format_instant(start_dt), format_instant(end_dt)


def _aware_datetime(value: str) -> datetime:
    try:
        return parse_instant(value)
    except ValueError as exc:
        raise EvalError(f"invalid datetime: {value}") from exc


def _raw_chat_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("chat_id") or raw.get("chatId")
    if isinstance(value, str) and value:
        return value
    chat = raw.get("chat")
    if isinstance(chat, dict):
        value = chat.get("chat_id") or chat.get("chatId") or chat.get("id")
        return value if isinstance(value, str) and value else None
    return None


def _dedupe_raws(raws: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raws:
        message_id = message_id_from_raw(raw)
        if not message_id:
            raise EvalError(
                "ingress live response contains a message without message_id"
            )
        by_id.setdefault(message_id, raw)
    return list(by_id.values())


def _write_review_markdown(directory: Path, *, judge_error: str | None) -> None:
    warning = f"\nJudge failed: {judge_error}\n" if judge_error else ""
    (directory / "REVIEW.md").write_text(
        """# Ingress Review

Review every row in `labels.review.yaml`. Edit only `expected_decision` and
`review_reason`. Matching decisions require an empty reason; mismatches require a
non-empty reason. Rows are sorted with proposed mismatches first.

Promote with `eval promote --type ingress --run <this-dir> --review labels.review.yaml --name <name>`.
"""
        + warning,
        encoding="utf-8",
    )
