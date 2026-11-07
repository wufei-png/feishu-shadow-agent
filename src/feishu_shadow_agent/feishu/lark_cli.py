from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from ..types import LarkCliResult, MessagePage

Runner = Callable[[list[str], int], LarkCliResult]


class LarkCliCommandError(RuntimeError):
    """Preserve a failed CLI result for boundary-specific recovery and audit."""

    def __init__(self, result: LarkCliResult):
        super().__init__(result.error or "lark-cli command failed")
        self.result = result


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

    def build_user_p2p_chat_list(self, *, page_token: str | None = None) -> list[str]:
        argv = [
            self.path,
            "im",
            "+chat-list",
            "--as",
            "user",
            "--types",
            "p2p",
            "--user-id-type",
            "open_id",
            "--page-size",
            "100",
            "--json",
        ]
        _extend_option(argv, "--page-token", page_token)
        return argv

    def list_user_p2p_chats(self, *, page_token: str | None = None) -> MessagePage:
        result = self.run_json(self.build_user_p2p_chat_list(page_token=page_token))
        if not result.ok:
            raise LarkCliCommandError(result)
        data: Any = result.json_data
        if not isinstance(data, dict):
            raise ValueError("chat list response is not an object")
        source: Any = cast(dict[str, Any], data).get("data", data)
        if not isinstance(source, dict):
            raise ValueError("chat list response is missing data")
        source = cast(dict[str, Any], source)
        chats = source.get("chats")
        has_more = source.get("has_more")
        token = source.get("page_token")
        if not isinstance(chats, list) or not isinstance(has_more, bool):
            raise ValueError("chat list response lacks complete pagination metadata")
        if any(not isinstance(chat, dict) for chat in cast(list[object], chats)):
            raise ValueError("chat list contains malformed rows")
        if has_more and (not isinstance(token, str) or not token):
            raise ValueError("chat list is incomplete without a next page token")
        return MessagePage(
            items=cast(list[dict[str, Any]], chats),
            next_page_token=token if has_more and isinstance(token, str) else None,
            has_more=has_more,
            raw=cast(dict[str, Any], data),
        )

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

    def build_chat_members_bots(
        self, *, chat_id: str, as_identity: str = "user"
    ) -> list[str]:
        _validate_identity(as_identity)
        if not chat_id:
            raise ValueError("chat_id is required")
        return [
            self.path,
            "im",
            "chat.members",
            "bots",
            "--as",
            as_identity,
            "--json",
            "--chat-id",
            chat_id,
        ]

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
        reply_in_thread: bool = False,
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
        if reply_in_thread:
            argv.append("--reply-in-thread")
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

    def build_messages_send_card(
        self,
        *,
        as_identity: str,
        card: dict[str, Any],
        idempotency_key: str,
        chat_id: str | None = None,
        user_id: str | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        _validate_identity(as_identity)
        _validate_exactly_one(chat_id=chat_id, user_id=user_id)
        if not card:
            raise ValueError("card is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        argv = [
            self.path,
            "im",
            "+messages-send",
            "--as",
            as_identity,
            "--json",
            "--content",
            json.dumps(card, ensure_ascii=False, separators=(",", ":")),
            "--msg-type",
            "interactive",
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

    def owner_card(
        self,
        *,
        owner_open_id: str,
        card: dict[str, Any],
        idempotency_key: str,
        dry_run: bool = True,
    ) -> LarkCliResult:
        return self.run_json(
            self.build_messages_send_card(
                as_identity="bot",
                user_id=owner_open_id,
                card=card,
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
        reply_in_thread: bool = False,
        dry_run: bool = True,
    ) -> LarkCliResult:
        return self.run_json(
            self.build_messages_reply(
                as_identity=as_identity,
                message_id=message_id,
                text=text,
                idempotency_key=idempotency_key,
                reply_in_thread=reply_in_thread,
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
        chat_type: str | None,
        is_at_me: bool,
        start: str | None,
        end: str | None,
        chat_id: str | None = None,
        page_token: str | None = None,
        query: str = "",
        page_size: int = 50,
        page_limit: int = 1,
    ) -> MessagePage:
        if page_limit < 1:
            raise ValueError("page_limit must be at least 1")
        result = self.run_json(
            self.build_messages_search(
                chat_id=chat_id,
                chat_type=chat_type,
                is_at_me=is_at_me,
                start=start,
                end=end,
                page_token=page_token,
                query=query,
                page_size=page_size,
                page_all=page_limit > 1,
                page_limit=page_limit if page_limit > 1 else None,
            )
        )
        page = _message_page_from_result(result)
        if page_limit == 1:
            return page
        page_count = (
            page_limit
            if page.has_more
            else max(1, (len(page.items) + page_size - 1) // page_size)
        )
        return MessagePage(
            items=page.items,
            next_page_token=page.next_page_token,
            has_more=page.has_more,
            raw=page.raw,
            page_count=page_count,
            warnings=page.warnings,
        )

    def search_owner_messages(
        self,
        *,
        sender: str,
        start: str | None,
        end: str | None,
        chat_type: str | None = None,
        chat_id: str | None = None,
        page_token: str | None = None,
        page_all: bool = True,
    ) -> MessagePage:
        result = self.run_json(
            self.build_messages_search(
                sender=sender,
                chat_type=chat_type,
                chat_id=chat_id,
                start=start,
                end=end,
                page_token=page_token,
                page_all=page_all,
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

    def list_chat_bots(
        self, *, chat_id: str, as_identity: str = "user"
    ) -> LarkCliResult:
        return self.run_json(
            self.build_chat_members_bots(chat_id=chat_id, as_identity=as_identity)
        )

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
        if parse_json and result.json_data is None:
            stdout = _json_stdout(result.stdout)
            if stdout:
                try:
                    json_data: Any = json.loads(stdout)
                except json.JSONDecodeError as exc:
                    if result.ok:
                        return LarkCliResult(
                            argv=result.argv,
                            exit_code=result.exit_code,
                            stdout=result.stdout,
                            stderr=result.stderr,
                            error=f"stdout was not valid JSON: {exc}",
                        )
                else:
                    error = result.error
                    if _cli_reported_error(json_data):
                        error = error or "lark-cli reported an error"
                    return LarkCliResult(
                        argv=result.argv,
                        exit_code=result.exit_code,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        json_data=json_data,
                        error=error,
                        timed_out=result.timed_out,
                    )
            elif result.ok:
                return LarkCliResult(
                    argv=result.argv,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    error="stdout was empty instead of JSON",
                )
        if parse_json and result.ok and _cli_reported_error(result.json_data):
            return replace(result, error="lark-cli reported an error")
        return result


def _run_subprocess(
    argv: list[str], timeout_seconds: int, *, cwd: Path | None = None
) -> LarkCliResult:
    try:
        # lark-cli receives a structured argv list and shell=False.
        completed = subprocess.run(  # noqa: S603
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
            stdout=_decode_command_output(exc.stdout),
            stderr=_decode_command_output(exc.stderr),
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
    return _json_stdout_and_warnings(stdout)[0]


def _json_stdout_and_warnings(stdout: str) -> tuple[str, tuple[str, ...]]:
    text = stdout.strip()
    if text.startswith("=== Dry Run ==="):
        text = text.removeprefix("=== Dry Run ===").strip()
    # Only discard the known, nonfatal lark-cli warning. Do not search for a
    # JSON fragment inside arbitrary output or accept extra trailing content.
    lines = text.splitlines()
    warnings: list[str] = []
    while lines and _is_known_warning(lines[0]):
        warnings.append(lines[0].strip())
        lines.pop(0)
    text = "\n".join(lines).strip()
    return text, tuple(warnings)


def _is_known_warning(line: str) -> bool:
    warning = "warning: reactions_partial_failed"
    stripped = line.strip()
    return stripped == warning or stripped.startswith(f"{warning}:")


def _cli_reported_error(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    envelope = cast(dict[str, Any], data)
    if envelope.get("ok") is False:
        return True
    code = envelope.get("code")
    return (isinstance(code, int) and not isinstance(code, bool) and code != 0) or (
        isinstance(code, str) and code.isdecimal() and int(code) != 0
    )


def _message_page_from_result(result: LarkCliResult) -> MessagePage:
    if not result.ok:
        raise LarkCliCommandError(result)
    try:
        page = _extract_message_page(result.json_data)
        _, stdout_warnings = _json_stdout_and_warnings(result.stdout)
        stderr_warnings = tuple(
            line.strip()
            for line in result.stderr.splitlines()
            if _is_known_warning(line)
        )
        return replace(page, warnings=stdout_warnings + stderr_warnings)
    except ValueError as exc:
        raise LarkCliCommandError(
            replace(result, error=f"invalid message page: {exc}")
        ) from exc


def _extract_message_page(data: Any) -> MessagePage:
    source: Any = data
    pagination_sources: list[dict[str, Any]] = []
    if isinstance(source, dict):
        source_map = cast(dict[str, Any], source)
        pagination_sources.append(source_map)
        if source_map.get("ok") is False:
            raise ValueError("CLI reported an error")
        code = source_map.get("code")
        if code not in (None, 0, "0"):
            raise ValueError(f"API returned code {code}")
        nested = source_map.get("data")
        if isinstance(nested, dict):
            source = cast(dict[str, Any], nested)
            pagination_sources.append(source)
    if isinstance(source, list):
        return MessagePage(items=_message_items(cast(list[object], source)), raw=data)
    if not isinstance(source, dict):
        raise ValueError("expected a message list or page object")

    source_map = cast(dict[str, Any], source)
    items = _extract_items(source_map)
    tokens: set[str] = set()
    has_more = False
    for pagination in pagination_sources:
        token = _string_or_none(
            pagination.get("page_token")
            or pagination.get("next_page_token")
            or pagination.get("next_page")
            or pagination.get("nextPageToken")
        )
        if token:
            tokens.add(token)
        has_more = _page_has_more(pagination.get("has_more")) or has_more
    if len(tokens) > 1:
        raise ValueError("conflicting next page tokens")
    next_page_token = next(iter(tokens), None)
    has_more = has_more or bool(next_page_token)
    if has_more and not next_page_token:
        raise ValueError("has_more is true without a next page token")
    return MessagePage(
        items=items, next_page_token=next_page_token, has_more=has_more, raw=data
    )


def _extract_items(source: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "messages", "message_list"):
        if key in source:
            value = source[key]
            if not isinstance(value, list):
                raise ValueError(f"{key} must be a list")
            return _message_items(cast(list[object], value))
    data = source.get("data")
    if isinstance(data, list):
        return _message_items(cast(list[object], data))
    raise ValueError("message list is missing")


def _message_items(items: list[object]) -> list[dict[str, Any]]:
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("message list contains a non-object item")
    return cast(list[dict[str, Any]], items)


def _decode_command_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value or None


def _page_has_more(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    raise ValueError("has_more is invalid")
