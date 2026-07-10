from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..types import LarkCliResult, MessagePage

Runner = Callable[[list[str], int], LarkCliResult]


class LarkCliClient:
    def __init__(
        self,
        *,
        path: str | None = None,
        timeout_seconds: int = 30,
        cwd: str | Path | None = None,
        runner: Runner | None = None,
    ):
        self.path = path or "lark-cli"
        self.timeout_seconds = timeout_seconds
        self.cwd = None if cwd is None else Path(cwd)
        self._runner = runner

    def build_version(self) -> list[str]:
        return [self.path, "--version"]

    def build_auth_status(self, *, verify: bool = True) -> list[str]:
        argv = [self.path, "auth", "status", "--json"]
        if verify:
            argv.append("--verify")
        return argv

    def build_messages_search(
        self,
        *,
        as_identity: str = "user",
        chat_id: str | None = None,
        chat_type: str | None = None,
        is_at_me: bool = False,
        start: str | None = None,
        end: str | None = None,
        page_all: bool = False,
        page_limit: int | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
        query: str | None = None,
        sender: str | None = None,
        exclude_sender_type: str | None = None,
        no_reactions: bool = True,
    ) -> list[str]:
        if as_identity != "user":
            # The search shortcut is user-only in the MVP path; failing locally
            # keeps bot visibility assumptions out of ingestion.
            raise ValueError("+messages-search is user-only")
        argv = [self.path, "im", "+messages-search", "--as", "user", "--json"]
        _extend_option(argv, "--chat-id", chat_id)
        _extend_option(argv, "--chat-type", chat_type)
        _extend_option(argv, "--start", start)
        _extend_option(argv, "--end", end)
        _extend_option(argv, "--page-token", page_token)
        _extend_option(argv, "--query", query)
        _extend_option(argv, "--sender", sender)
        _extend_option(argv, "--exclude-sender-type", exclude_sender_type)
        if page_size is not None:
            _extend_option(argv, "--page-size", str(page_size))
        if page_limit is not None:
            _extend_option(argv, "--page-limit", str(page_limit))
        if is_at_me:
            argv.append("--is-at-me")
        if page_all:
            argv.append("--page-all")
        if no_reactions:
            argv.append("--no-reactions")
        return argv

    def build_chat_messages_list(
        self,
        *,
        as_identity: str,
        chat_id: str | None = None,
        user_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        order: str = "asc",
        page_size: int = 50,
        page_token: str | None = None,
        no_reactions: bool = True,
    ) -> list[str]:
        _validate_identity(as_identity)
        _validate_exactly_one(chat_id=chat_id, user_id=user_id)
        if user_id and as_identity != "user":
            raise ValueError("--user-id P2P resolution is user identity only")
        if order not in {"asc", "desc"}:
            raise ValueError("order must be asc or desc")
        argv = [self.path, "im", "+chat-messages-list", "--as", as_identity, "--json"]
        _extend_option(argv, "--chat-id", chat_id)
        _extend_option(argv, "--user-id", user_id)
        _extend_option(argv, "--start", start)
        _extend_option(argv, "--end", end)
        _extend_option(argv, "--order", order)
        _extend_option(argv, "--page-size", str(page_size))
        _extend_option(argv, "--page-token", page_token)
        if no_reactions:
            argv.append("--no-reactions")
        return argv

    def build_threads_messages_list(
        self,
        *,
        as_identity: str,
        thread: str,
        order: str = "asc",
        page_size: int = 50,
        page_token: str | None = None,
        no_reactions: bool = True,
    ) -> list[str]:
        _validate_identity(as_identity)
        if not thread:
            raise ValueError("thread is required")
        if order not in {"asc", "desc"}:
            raise ValueError("order must be asc or desc")
        argv = [
            self.path,
            "im",
            "+threads-messages-list",
            "--as",
            as_identity,
            "--json",
            "--thread",
            thread,
            "--order",
            order,
            "--page-size",
            str(page_size),
        ]
        _extend_option(argv, "--page-token", page_token)
        if no_reactions:
            argv.append("--no-reactions")
        return argv

    def build_messages_reply(
        self,
        *,
        as_identity: str,
        message_id: str,
        text: str,
        idempotency_key: str,
        dry_run: bool = True,
    ) -> list[str]:
        _validate_identity(as_identity)
        if not message_id:
            raise ValueError("message_id is required")
        if not text:
            raise ValueError("text is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        argv = [
            self.path,
            "im",
            "+messages-reply",
            "--as",
            as_identity,
            "--json",
            "--message-id",
            message_id,
            "--text",
            text,
            "--idempotency-key",
            idempotency_key,
        ]
        if dry_run:
            argv.append("--dry-run")
        return argv

    def build_messages_send(
        self,
        *,
        as_identity: str,
        text: str,
        idempotency_key: str,
        chat_id: str | None = None,
        user_id: str | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        _validate_identity(as_identity)
        _validate_exactly_one(chat_id=chat_id, user_id=user_id)
        if not text:
            raise ValueError("text is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        argv = [
            self.path,
            "im",
            "+messages-send",
            "--as",
            as_identity,
            "--json",
            "--text",
            text,
            "--idempotency-key",
            idempotency_key,
        ]
        _extend_option(argv, "--chat-id", chat_id)
        _extend_option(argv, "--user-id", user_id)
        if dry_run:
            argv.append("--dry-run")
        return argv

    def build_messages_mget(
        self,
        *,
        as_identity: str,
        message_ids: Sequence[str],
        dry_run: bool = False,
        no_reactions: bool = True,
    ) -> list[str]:
        _validate_identity(as_identity)
        ids = [message_id for message_id in message_ids if message_id]
        if not ids:
            raise ValueError("message_ids are required")
        if len(ids) > 50:
            raise ValueError("messages-mget supports at most 50 message IDs")
        argv = [
            self.path,
            "im",
            "+messages-mget",
            "--as",
            as_identity,
            "--json",
            "--message-ids",
            ",".join(ids),
        ]
        if no_reactions:
            argv.append("--no-reactions")
        if dry_run:
            argv.append("--dry-run")
        return argv

    def build_resources_download(
        self,
        *,
        as_identity: str,
        message_id: str,
        file_key: str,
        resource_type: str,
        output: str | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        _validate_identity(as_identity)
        if resource_type not in {"image", "file"}:
            raise ValueError("resource_type must be image or file")
        if not message_id:
            raise ValueError("message_id is required")
        if not file_key:
            raise ValueError("file_key is required")
        if output is not None:
            _validate_safe_relative_output(output)
        argv = [
            self.path,
            "im",
            "+messages-resources-download",
            "--as",
            as_identity,
            "--json",
            "--message-id",
            message_id,
            "--file-key",
            file_key,
            "--type",
            resource_type,
        ]
        _extend_option(argv, "--output", output)
        if dry_run:
            argv.append("--dry-run")
        return argv

    def version(self) -> LarkCliResult:
        return self.run_text(self.build_version())

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return self.run_json(self.build_auth_status(verify=verify))

    def owner_message(
        self,
        *,
        owner_open_id: str,
        text: str,
        idempotency_key: str,
        dry_run: bool = True,
    ) -> LarkCliResult:
        return self.run_json(
            self.build_messages_send(
                as_identity="bot",
                user_id=owner_open_id,
                text=text,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )
        )

    def reply_message(
        self,
        *,
        as_identity: str,
        message_id: str,
        text: str,
        idempotency_key: str,
        dry_run: bool = True,
    ) -> LarkCliResult:
        return self.run_json(
            self.build_messages_reply(
                as_identity=as_identity,
                message_id=message_id,
                text=text,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )
        )

    def get_messages(
        self,
        *,
        as_identity: str,
        message_ids: list[str],
    ) -> MessagePage:
        result = self.run_json(
            self.build_messages_mget(
                as_identity=as_identity,
                message_ids=message_ids,
                no_reactions=True,
            )
        )
        return _message_page_from_result(result)

    def search_messages(
        self,
        *,
        chat_type: str,
        is_at_me: bool,
        start: str | None,
        end: str | None,
        page_token: str | None = None,
        query: str = "",
        page_size: int = 50,
    ) -> MessagePage:
        result = self.run_json(
            self.build_messages_search(
                chat_type=chat_type,
                is_at_me=is_at_me,
                start=start,
                end=end,
                page_token=page_token,
                query=query,
                page_size=page_size,
            )
        )
        return _message_page_from_result(result)

    def search_owner_messages(
        self,
        *,
        sender: str,
        start: str | None,
        end: str | None,
    ) -> MessagePage:
        result = self.run_json(
            self.build_messages_search(
                sender=sender,
                start=start,
                end=end,
                page_all=True,
                query="",
                no_reactions=True,
            )
        )
        return _message_page_from_result(result)

    def list_chat_messages(
        self,
        *,
        chat_id: str,
        start: str | None,
        end: str | None,
        page_token: str | None = None,
        page_size: int = 50,
        order: str = "asc",
    ) -> MessagePage:
        result = self.run_json(
            self.build_chat_messages_list(
                as_identity="user",
                chat_id=chat_id,
                start=start,
                end=end,
                order=order,
                page_token=page_token,
                page_size=page_size,
            )
        )
        return _message_page_from_result(result)

    def list_p2p_messages(
        self,
        *,
        user_id: str,
        start: str | None,
        end: str | None,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> MessagePage:
        result = self.run_json(
            self.build_chat_messages_list(
                as_identity="user",
                user_id=user_id,
                start=start,
                end=end,
                order="asc",
                page_token=page_token,
                page_size=page_size,
            )
        )
        return _message_page_from_result(result)

    def list_thread_messages(
        self,
        *,
        thread_id: str,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> MessagePage:
        result = self.run_json(
            self.build_threads_messages_list(
                as_identity="user",
                thread=thread_id,
                order="asc",
                page_token=page_token,
                page_size=page_size,
            )
        )
        return _message_page_from_result(result)

    def download_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        output: str,
    ) -> LarkCliResult:
        return self.run_json(
            self.build_resources_download(
                as_identity="bot",
                message_id=message_id,
                file_key=file_key,
                resource_type=resource_type,
                output=output,
                dry_run=False,
            )
        )

    def run_text(self, argv: Sequence[str]) -> LarkCliResult:
        return self._run(list(argv), parse_json=False)

    def run_json(self, argv: Sequence[str]) -> LarkCliResult:
        return self._run(list(argv), parse_json=True)

    def _run(self, argv: list[str], *, parse_json: bool) -> LarkCliResult:
        if self._runner is not None:
            result = self._runner(argv, self.timeout_seconds)
        else:
            result = _run_subprocess(argv, self.timeout_seconds, cwd=self.cwd)
        if parse_json and result.ok:
            if result.json_data is not None:
                return result
            try:
                json_data: Any = json.loads(_json_stdout(result.stdout) or "{}")
            except json.JSONDecodeError as exc:
                return LarkCliResult(
                    argv=result.argv,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    error=f"stdout was not valid JSON: {exc}",
                )
            return LarkCliResult(
                argv=result.argv,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                json_data=json_data,
            )
        return result


def _run_subprocess(
    argv: list[str], timeout_seconds: int, *, cwd: Path | None = None
) -> LarkCliResult:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return LarkCliResult(
            argv=argv,
            exit_code=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            error=f"command timed out after {timeout_seconds}s",
            timed_out=True,
        )
    except OSError as exc:
        return LarkCliResult(argv=argv, exit_code=None, error=str(exc))
    if completed.returncode != 0:
        return LarkCliResult(
            argv=argv,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=completed.stderr.strip()
            or completed.stdout.strip()
            or "command failed",
        )
    return LarkCliResult(
        argv=argv,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _validate_identity(value: str) -> None:
    if value not in {"user", "bot"}:
        raise ValueError("identity must be user or bot")


def _validate_exactly_one(**values: str | None) -> None:
    present = [key for key, value in values.items() if value]
    if len(present) != 1:
        names = ", ".join(values)
        raise ValueError(f"exactly one of {names} is required")


def _validate_safe_relative_output(value: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("output must be a safe relative path")


def _extend_option(argv: list[str], option: str, value: str | None) -> None:
    if value is not None:
        argv.extend([option, value])


def _json_stdout(stdout: str) -> str:
    text = stdout.strip()
    if text.startswith("=== Dry Run ==="):
        text = text.removeprefix("=== Dry Run ===").strip()
    return text


def _message_page_from_result(result: LarkCliResult) -> MessagePage:
    if not result.ok:
        raise RuntimeError(result.error or "lark-cli command failed")
    return _extract_message_page(result.json_data)


def _extract_message_page(data: Any) -> MessagePage:
    source = data
    if isinstance(source, dict) and isinstance(source.get("data"), dict):
        source = source["data"]
    if isinstance(source, list):
        return MessagePage(
            items=[item for item in source if isinstance(item, dict)], raw=data
        )
    if not isinstance(source, dict):
        return MessagePage(items=[], raw=data)

    items = _extract_items(source)
    next_page_token = _string_or_none(
        source.get("page_token")
        or source.get("next_page_token")
        or source.get("next_page")
        or source.get("nextPageToken")
    )
    has_more = _truthy(source.get("has_more")) or bool(next_page_token)
    return MessagePage(
        items=items, next_page_token=next_page_token, has_more=has_more, raw=data
    )


def _extract_items(source: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "messages", "message_list"):
        value = source.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if isinstance(source.get("data"), list):
        return [item for item in source["data"] if isinstance(item, dict)]
    return []


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value or None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return False
