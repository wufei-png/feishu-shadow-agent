from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import AppConfig
from .feishu.lark_cli import LarkCliCommandError
from .jsonl import JSONLLogger
from .membership import (
    classify_bot_membership_absence,
    record_bot_membership_absence,
)
from .message_eligibility import MessageEligibilityPolicy
from .paths import resolve_agent_working_dir
from .policy import PolicyResolver
from .processing import ApprovalService, TaskProcessingService
from .routing import MessageRouter, RoutingResult
from .store.sqlite_store import SQLiteStore
from .time_utils import (
    format_instant,
    normalize_instant,
    parse_instant,
    parse_instant_or_none,
    shift_instant,
)
from .types import (
    LarkCliResult,
    MessagePage,
    NormalizedMessage,
    ResourceRef,
    ResourceStatus,
    RouteDecision,
    RouteName,
    SenderRole,
    utc_now_iso,
)


class IngestionFeishuClient(Protocol):
    def auth_status(self, *, verify: bool = True) -> LarkCliResult: ...

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
        page_limit: int = 1,
    ) -> MessagePage: ...

    def list_chat_messages(
        self,
        *,
        chat_id: str,
        start: str | None,
        end: str | None,
        page_token: str | None = None,
        page_size: int = 50,
        order: str = "asc",
    ) -> MessagePage: ...

    def list_p2p_messages(
        self,
        *,
        user_id: str,
        start: str | None,
        end: str | None,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> MessagePage: ...

    def list_thread_messages(
        self,
        *,
        thread_id: str,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> MessagePage: ...

    download_resource: Callable[..., LarkCliResult]


PAGE_SIZE = 50
MAX_PROCESSING_RESERVE_SECONDS = 5.0
FEISHU_MESSAGE_TIMEZONE = ZoneInfo("Asia/Shanghai")
IMAGE_KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(img_[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)
FILE_KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(file_[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)
AT_USER_ID_PATTERN = re.compile(
    r"<at\s+[^>]*user_id=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE
)
PARTIAL_RESOURCE_PATTERN = re.compile(r"^.+\.bin\.part-[0-9a-f]{32}$")


@dataclass(frozen=True)
class StageResult:
    name: str
    ok: bool
    processed: int = 0
    error: str | None = None


@dataclass(frozen=True)
class DrainWindow:
    checkpoint_key: str
    checkpoint: dict[str, Any]
    start: str
    end: str
    page_token: str | None


@dataclass(frozen=True)
class DrainResult:
    items: list[dict[str, Any]]
    complete: bool
    next_page_token: str | None
    pages: int
    reason: str | None = None
    fetch_reason: str | None = None


@dataclass(frozen=True)
class ProcessingCursor:
    """A replay-safe prefix of one fetched drain batch.

    The cursor contains no raw message content.  The next tick re-fetches the
    batch from the same page token and only skips this prefix after checking a
    digest of its normalized routing inputs again.  A changed result is
    replayed from its beginning rather than risk skipping a message.
    """

    completed_items: int
    prefix_sha256: str


@dataclass(frozen=True)
class RawBatchResult:
    """Outcome of a bounded raw-message pass.

    ``processed`` follows the routing-stage convention: it counts calls that
    produced a routing result.  Some consumers, notably approval inbox, handle
    a raw command successfully without producing one, so ``handled_items``
    retains the independent safe-boundary count for their stage result.
    """

    processed: int
    handled_items: int
    complete: bool
    cursor: ProcessingCursor | None = None


@dataclass(frozen=True)
class ResourceQuotaDecision:
    allow: bool
    status: str | None = None
    raw: dict[str, Any] | None = None


def _token_fingerprint(token: str | None) -> str | None:
    return None if token is None else sha256(token.encode()).hexdigest()[:16]


def _json_error_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    error = cast(dict[str, Any], value)
    return {
        key: error[key]
        for key in ("code", "msg", "message", "request_id")
        if isinstance(error.get(key), (str, int))
    }


def _redacted_cli_argv(argv: list[str]) -> list[str]:
    sensitive = {"--page-token", "--chat-id", "--user-id"}
    result = list(argv)
    for index, value in enumerate(result[:-1]):
        if value in sensitive:
            result[index + 1] = "<redacted>"
    return result


def normalize_message_sent_at(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_instant(value)
    except ValueError as exc:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise exc from None
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            raise exc
        # lark-cli renders Feishu message timestamps in China Standard Time
        # without an offset. Resolve that at the adapter boundary so domain
        # time never depends on the host timezone.
        return format_instant(parsed.replace(tzinfo=FEISHU_MESSAGE_TIMEZONE))


class MessageNormalizer:
    def __init__(self, *, owner_open_id: str):
        self.owner_open_id = owner_open_id

    def normalize(
        self, raw: dict[str, Any], *, default_chat_type: str | None = None
    ) -> NormalizedMessage:
        message_id = _first_string(raw, "message_id", "messageId", "id") or ""
        if not message_id:
            raise ValueError("message is missing message_id")
        content = _content(raw)
        sender_value = raw.get("sender")
        sender = (
            cast(dict[str, Any], sender_value) if isinstance(sender_value, dict) else {}
        )
        sender_id = _first_string(
            raw, "sender_id", "senderId", "open_id", "openId"
        ) or _first_string(sender, "sender_id", "senderId", "open_id", "openId", "id")
        profile_value = sender.get("profile")
        profile = (
            cast(dict[str, Any], profile_value)
            if isinstance(profile_value, dict)
            else {}
        )
        sender_name = (
            _first_string(
                raw, "sender_name", "senderName", "user_name", "userName", "name"
            )
            or _first_string(
                sender, "sender_name", "senderName", "user_name", "userName", "name"
            )
            or _first_string(
                profile,
                "name",
                "display_name",
                "displayName",
            )
            or sender_id
        )
        sender_type = _first_string(raw, "sender_type", "senderType") or _first_string(
            sender, "sender_type", "senderType", "type"
        )
        chat_value = raw.get("chat")
        chat = cast(dict[str, Any], chat_value) if isinstance(chat_value, dict) else {}
        chat_id = _first_string(raw, "chat_id", "chatId") or _first_string(
            chat, "chat_id", "chatId", "id"
        )
        chat_type = _first_string(raw, "chat_type", "chatType") or default_chat_type
        if chat_type not in {"group", "p2p"}:
            chat_type = None
        sent_at = normalize_message_sent_at(
            _first_string(raw, "create_time", "created_at", "sent_at", "timestamp")
        )
        thread_id = _thread_id(raw)
        reply_to_value = raw.get("reply_to") or raw.get("replyTo")
        reply_to = (
            cast(dict[str, Any], reply_to_value)
            if isinstance(reply_to_value, dict)
            else {}
        )
        reply_to_message_id = _first_string(
            raw,
            "reply_to_message_id",
            "replyToMessageId",
            "reply_to",
            "replyTo",
            "parent_id",
            "parentId",
            "root_id",
            "rootId",
        ) or _first_string(
            reply_to,
            "message_id",
            "messageId",
            "id",
        )
        text = _message_text(raw, content)
        mentions = _mentions(raw, content)
        at_all = _is_at_all(raw, text, mentions)
        direct_mention = bool(
            raw.get("is_at_me") or raw.get("isAtMe") or self.owner_open_id in mentions
        )
        if at_all:
            direct_mention = False
        return NormalizedMessage(
            message_id=message_id,
            chat_id=chat_id,
            chat_type=chat_type,  # type: ignore[arg-type]
            sender_id=sender_id,
            sender_name=sender_name,
            sender_type=sender_type,
            sender_role=self._sender_role(
                sender_id=sender_id, sender_type=sender_type, raw=raw
            ),
            sent_at=sent_at,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            text=text,
            direct_mention=direct_mention,
            at_all=at_all,
            mentions=mentions,
            resources=_resources(message_id, raw, content),
            message_type=_message_type(raw, content),
            raw=raw,
            is_deleted=_is_deleted(raw, content),
        )

    def _sender_role(
        self, *, sender_id: str | None, sender_type: str | None, raw: dict[str, Any]
    ) -> SenderRole:
        lowered_type = (sender_type or "").lower()
        if raw.get("sent_by_agent") is True or raw.get("agent_message") is True:
            return "agent_message"
        if lowered_type in {"bot", "app"}:
            return "bot_message"
        if sender_id == self.owner_open_id:
            return "owner_message"
        return "external_user_message"


class ResourceProcessor:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        feishu_client: IngestionFeishuClient,
        config: AppConfig,
        logger: JSONLLogger,
        config_base_dir: str | Path | None = None,
        resource_base_dir: str | Path | None = None,
        store_absolute_paths: bool = False,
        preserve_resource_base_path: bool = False,
    ):
        self.store = store
        self.feishu_client = feishu_client
        self.config = config
        self.logger = logger
        self.config_base_dir = (
            Path(config_base_dir or Path.cwd()).expanduser().resolve()
        )
        resource_base = Path(resource_base_dir or self.config_base_dir).expanduser()
        self.resource_base_dir = (
            resource_base.absolute()
            if preserve_resource_base_path
            else resource_base.resolve()
        )
        self.store_absolute_paths = store_absolute_paths
        self.policy = PolicyResolver(store)
        self.quota = ResourceQuotaGuard(config=config, base_dir=self.resource_base_dir)
        self._quota_downloads_blocked = False

    def process(self, message: NormalizedMessage, *, run_id: str | None = None) -> None:
        if not message.resources:
            return
        stale_parts_deleted = self.quota.cleanup_stale_partial_downloads()
        if stale_parts_deleted:
            self.logger.info(
                "stale_partial_resources_deleted",
                run_id=run_id,
                data={"count": stale_parts_deleted},
            )
        existing_resources = {
            (str(row["file_key"]), str(row["resource_type"])): row
            for row in self.store.list_resources_for_messages([message.message_id])
        }
        resource_policy = self.policy.can_download_resources(message)
        for resource in message.resources:
            if (
                not resource_policy.allow
                and resource_policy.reason == "disabled_by_chat_policy"
            ):
                self.store.upsert_resource(
                    resource,
                    download_status="skipped",
                    raw={
                        "reason": "disabled_by_chat_policy",
                        "policy_source": resource_policy.policy_source,
                    },
                )
                self.logger.info(
                    "resource_download_skipped",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "reason": "disabled_by_chat_policy",
                        "policy_source": resource_policy.policy_source,
                    },
                )
                continue
            if not resource_policy.allow and resource_policy.reason == "bot_not_joined":
                # Resource download is bot-only for user messages. Recording the
                # gate here avoids repeated 234040/234002 calls and lets reply
                # policy decide whether the task can proceed without the asset.
                self.store.upsert_resource(
                    resource,
                    download_status="bot_not_joined",
                    raw={
                        "reason": "chat_policy_bot_joined_false",
                        "policy_source": resource_policy.policy_source,
                    },
                )
                self.logger.warning(
                    "resource_download_skipped",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "reason": "bot_not_joined",
                        "policy_source": resource_policy.policy_source,
                    },
                )
                continue
            if self._quota_downloads_blocked:
                self._record_quota_blocked(
                    resource,
                    run_id=run_id,
                    decision=ResourceQuotaDecision(
                        allow=False,
                        status=ResourceStatus.QUOTA_EXCEEDED.value,
                        raw={
                            "reason": "resource_quota_exceeded",
                            "blocked_after_prior_quota": True,
                        },
                    ),
                )
                continue
            output = _resource_output(resource, self.config.storage.resource_dir)
            local_output = self.resource_base_dir / output
            stored_path = str(local_output) if self.store_absolute_paths else output
            existing = existing_resources.get(
                (resource.file_key, resource.resource_type)
            )
            if self._verified_existing_download(
                existing,
                local_output=local_output,
                stored_path=stored_path,
            ):
                self.logger.debug(
                    "resource_download_skipped",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "reason": "already_downloaded",
                    },
                )
                continue
            quota_preflight = self.quota.before_download()
            if not quota_preflight.allow:
                self._record_quota_blocked(
                    resource, run_id=run_id, decision=quota_preflight
                )
                self._quota_downloads_blocked = True
                continue
            self.logger.debug(
                "resource_download_started",
                run_id=run_id,
                data={
                    "message_id": resource.message_id,
                    "file_key": resource.file_key,
                    "resource_type": resource.resource_type,
                    "output": output,
                },
            )
            temporary_output = f"{output}.part-{uuid4().hex}"
            temporary_path = self.resource_base_dir / temporary_output
            try:
                self.quota.validate_download_target(local_output)
                self.quota.validate_download_target(temporary_path)
                local_output.parent.mkdir(parents=True, exist_ok=True)
                self.quota.validate_download_target(local_output)
                self.quota.validate_download_target(temporary_path)
                result = self.feishu_client.download_resource(
                    message_id=resource.message_id,
                    file_key=resource.file_key,
                    resource_type=resource.resource_type,
                    output=temporary_output,
                )
            except Exception as exc:  # noqa: BLE001
                # Feishu resource clients can expose transport-specific
                # exceptions; mark this resource failed and continue ingestion.
                self.quota.delete_downloaded_file(temporary_path)
                self.store.upsert_resource(
                    resource,
                    download_status="failed",
                    path=None,
                    raw={"error": str(exc)},
                )
                self.logger.warning(
                    "resource_download_failed",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "error": str(exc),
                    },
                )
                continue
            result_json = _normalized_download_result(
                result.json_data,
                temporary_output=temporary_output,
                final_output=output,
            )
            try:
                self.quota.validate_download_target(temporary_path)
            except ValueError as exc:
                self.quota.delete_downloaded_file(temporary_path)
                self.store.upsert_resource(
                    resource,
                    download_status="failed",
                    path=None,
                    raw={"error": str(exc), "phase": "download_validation"},
                )
                self.logger.warning(
                    "resource_download_failed",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "error": str(exc),
                        "phase": "download_validation",
                    },
                )
                continue
            if result.ok:
                if not temporary_path.exists() or not temporary_path.is_file():
                    self.store.upsert_resource(
                        resource,
                        download_status="missing_file",
                        path=None,
                        raw={
                            "result": result_json,
                            "error": "download output file missing",
                        },
                    )
                    self.logger.warning(
                        "resource_download_missing_file",
                        run_id=run_id,
                        data={
                            "message_id": resource.message_id,
                            "file_key": resource.file_key,
                            "path": output,
                        },
                    )
                    continue
                quota_result = self.quota.after_download(
                    temporary_path,
                    attempted_path=output,
                    replacing_path=local_output,
                )
                if not quota_result.allow:
                    raw = {"result": result_json} | (quota_result.raw or {})
                    self._record_quota_blocked(
                        resource, run_id=run_id, decision=quota_result, raw=raw
                    )
                    if quota_result.status == ResourceStatus.QUOTA_EXCEEDED.value:
                        self._quota_downloads_blocked = True
                    continue
                sha256_hex = _sha256_if_exists(temporary_path)
                if sha256_hex is None:
                    self.store.upsert_resource(
                        resource,
                        download_status="missing_file",
                        path=None,
                        raw={
                            "result": result_json,
                            "error": "download output file missing",
                        },
                    )
                    self.logger.warning(
                        "resource_download_missing_file",
                        run_id=run_id,
                        data={
                            "message_id": resource.message_id,
                            "file_key": resource.file_key,
                            "path": output,
                        },
                    )
                    continue
                try:
                    _publish_download(temporary_path, local_output)
                except OSError as exc:
                    self.quota.delete_downloaded_file(temporary_path)
                    self.store.upsert_resource(
                        resource,
                        download_status="failed",
                        path=None,
                        raw={"error": str(exc), "phase": "atomic_publish"},
                    )
                    self.logger.warning(
                        "resource_download_failed",
                        run_id=run_id,
                        data={
                            "message_id": resource.message_id,
                            "file_key": resource.file_key,
                            "error": str(exc),
                            "phase": "atomic_publish",
                        },
                    )
                    continue
                self.store.upsert_resource(
                    resource,
                    download_status="downloaded",
                    path=stored_path,
                    sha256_hex=sha256_hex,
                    raw={"result": result_json},
                )
                self.logger.info(
                    "resource_downloaded",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "path": output,
                        "sha256": sha256_hex,
                    },
                )
            else:
                self.quota.delete_downloaded_file(temporary_path)
                membership_absence = classify_bot_membership_absence(result)
                status = "bot_invisible" if membership_absence else "failed"
                self.store.upsert_resource(
                    resource,
                    download_status=status,
                    path=None,
                    raw={
                        "error": result.error,
                        "stderr": result.stderr,
                        "stdout": result.stdout,
                    },
                )
                self.logger.warning(
                    "resource_download_unavailable",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "status": status,
                        "error": result.error,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                    },
                )
                if membership_absence:
                    record_bot_membership_absence(
                        store=self.store,
                        config=self.config,
                        logger=self.logger,
                        chat_id=message.chat_id,
                        chat_type=message.chat_type,
                        run_id=run_id,
                        source="resource_download_failure",
                        error=result.error or result.stderr,
                        absence=membership_absence,
                    )

    def _verified_existing_download(
        self,
        existing: Any,
        *,
        local_output: Path,
        stored_path: str,
    ) -> bool:
        if (
            existing is None
            or existing["download_status"] != "downloaded"
            or existing["path"] != stored_path
            or not existing["sha256"]
        ):
            return False
        try:
            self.quota.validate_download_target(local_output)
        except ValueError:
            return False
        if local_output.is_symlink() or not local_output.is_file():
            return False
        return _sha256_if_exists(local_output) == existing["sha256"]

    def _record_quota_blocked(
        self,
        resource: ResourceRef,
        *,
        run_id: str | None,
        decision: ResourceQuotaDecision,
        raw: dict[str, Any] | None = None,
    ) -> None:
        status = decision.status or ResourceStatus.QUOTA_EXCEEDED.value
        payload = (decision.raw or {}) | (raw or {})
        self.store.upsert_resource(
            resource,
            download_status=status,
            path=None,
            raw=payload,
        )
        self.logger.warning(
            "resource_download_blocked_by_quota",
            run_id=run_id,
            data={
                "message_id": resource.message_id,
                "file_key": resource.file_key,
                "resource_type": resource.resource_type,
                "status": status,
                "details": payload,
            },
        )


class ResourceQuotaGuard:
    def __init__(self, *, config: AppConfig, base_dir: Path):
        self.config = config
        self.base_dir = base_dir
        self.resource_path = (base_dir / config.storage.resource_dir).absolute()
        self.resource_root = self.resource_path.resolve(strict=False)

    def validate_download_target(self, path: Path) -> None:
        try:
            relative = path.absolute().relative_to(self.resource_path)
        except ValueError as exc:
            raise ValueError("download target is outside resource directory") from exc
        current = self.resource_path
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("download target contains a symbolic link")
        try:
            path.resolve(strict=False).relative_to(self.resource_root)
        except ValueError as exc:
            raise ValueError("download target is outside resource directory") from exc

    def before_download(self) -> ResourceQuotaDecision:
        usage = self.resource_dir_usage_bytes()
        if usage >= self.config.storage.max_resource_dir_bytes:
            return ResourceQuotaDecision(
                allow=False,
                status=ResourceStatus.QUOTA_EXCEEDED.value,
                raw={
                    "reason": "resource_quota_exceeded",
                    "resource_dir_bytes": usage,
                    "max_resource_dir_bytes": self.config.storage.max_resource_dir_bytes,
                },
            )
        return ResourceQuotaDecision(allow=True)

    def cleanup_stale_partial_downloads(self, *, now: float | None = None) -> int:
        if not self.resource_root.exists():
            return 0
        stale_after_seconds = max(300, self.config.lark_cli.timeout_seconds * 2)
        stale_before = (time.time() if now is None else now) - stale_after_seconds
        deleted = 0
        for path in self.resource_root.rglob("*.part-*"):
            try:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or PARTIAL_RESOURCE_PATTERN.fullmatch(path.name) is None
                    or path.stat().st_mtime > stale_before
                ):
                    continue
                path.unlink()
            except OSError:
                continue
            deleted += 1
        return deleted

    def after_download(
        self,
        path: Path,
        *,
        attempted_path: str,
        replacing_path: Path | None = None,
    ) -> ResourceQuotaDecision:
        size = path.stat().st_size
        if size > self.config.storage.max_resource_bytes:
            delete_error = self.delete_downloaded_file(path)
            raw = {
                "reason": "resource_too_large",
                "attempted_path": attempted_path,
                "size_bytes": size,
                "max_resource_bytes": self.config.storage.max_resource_bytes,
            }
            if delete_error:
                raw["delete_error"] = delete_error
            return ResourceQuotaDecision(
                allow=False,
                status=ResourceStatus.TOO_LARGE.value,
                raw=raw,
            )
        replaced_size = 0
        if replacing_path is not None and replacing_path != path:
            self.validate_download_target(replacing_path)
            if not replacing_path.is_symlink() and replacing_path.is_file():
                replaced_size = replacing_path.stat().st_size
        usage = self.resource_dir_usage_bytes() - replaced_size
        if usage > self.config.storage.max_resource_dir_bytes:
            delete_error = self.delete_downloaded_file(path)
            raw = {
                "reason": "resource_quota_exceeded",
                "attempted_path": attempted_path,
                "size_bytes": size,
                "resource_dir_bytes": usage,
                "max_resource_dir_bytes": self.config.storage.max_resource_dir_bytes,
            }
            if delete_error:
                raw["delete_error"] = delete_error
            return ResourceQuotaDecision(
                allow=False,
                status=ResourceStatus.QUOTA_EXCEEDED.value,
                raw=raw,
            )
        return ResourceQuotaDecision(allow=True)

    def resource_dir_usage_bytes(self) -> int:
        if not self.resource_root.exists():
            return 0
        total = 0
        for path in self.resource_root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def delete_downloaded_file(self, path: Path) -> str | None:
        try:
            path.absolute().relative_to(self.resource_path)
        except ValueError:
            return "outside_resource_dir"
        if not path.is_symlink():
            try:
                path.resolve(strict=False).relative_to(self.resource_root)
            except ValueError:
                return "outside_resource_dir"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return str(exc)
        return None


class IngestionService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        feishu_client: IngestionFeishuClient,
        config: AppConfig,
        logger: JSONLLogger,
        router: MessageRouter | None = None,
        task_processor: TaskProcessingService | None = None,
        approval_service: ApprovalService | None = None,
        clock: Callable[[], str] = utc_now_iso,
        config_base_dir: str | Path | None = None,
        resource_base_dir: str | Path | None = None,
        store_absolute_resource_paths: bool = False,
        preserve_resource_base_path: bool = False,
        monotonic: Callable[[], float] | None = None,
    ):
        self.store = store
        self.feishu_client = feishu_client
        self.config = config
        self.logger = logger
        self.normalizer = MessageNormalizer(owner_open_id=config.owner.open_id)
        self.eligibility = MessageEligibilityPolicy()
        self.config_base_dir = (
            Path(config_base_dir or Path.cwd()).expanduser().resolve()
        )
        self.agent_working_dir = resolve_agent_working_dir(
            config.agent_backend.working_dir, self.config_base_dir
        )
        self.router = router or MessageRouter(
            store=store,
            closed_recall_days=config.lifecycle.closed_recall_days,
            burst_attach_seconds=config.lifecycle.burst_attach_seconds,
        )
        self.task_processor = task_processor
        self.approval_service = approval_service or (
            task_processor.approvals if task_processor is not None else None
        )
        self.resources = ResourceProcessor(
            store=store,
            feishu_client=feishu_client,
            config=config,
            logger=logger,
            config_base_dir=config_base_dir,
            resource_base_dir=resource_base_dir,
            store_absolute_paths=store_absolute_resource_paths,
            preserve_resource_base_path=preserve_resource_base_path,
        )
        if self.task_processor is not None:
            self.task_processor.set_resource_retry_func(
                lambda message, retry_run_id: self.resources.process(
                    message, run_id=retry_run_id
                )
            )
        self.clock = lambda: normalize_instant(clock())
        self.monotonic = monotonic or time.monotonic
        self.ingest_deadline = (
            self.monotonic() + config.daemon.ingest_tick_budget_seconds
        )
        # Do not let a sequence of individually successful page calls consume
        # the entire shared tick.  A bounded reserve lets the already-fetched
        # batch reach a durable processing boundary in the same tick.
        self.ingest_fetch_deadline = self.ingest_deadline - min(
            MAX_PROCESSING_RESERVE_SECONDS,
            config.daemon.ingest_tick_budget_seconds / 2,
        )

    def run_approval_inbox_placeholder(self, *, run_id: str) -> StageResult:
        self.logger.emit(
            "info",
            "approval_inbox_placeholder",
            run_id=run_id,
            data={"checkpoint": "approval_inbox", "checkpoint_written": False},
        )
        return StageResult("approval_inbox", ok=True)

    def run_processing_retries(self, *, run_id: str, limit: int = 20) -> StageResult:
        if self.task_processor is None:
            return StageResult("processing_retries", ok=True)
        processed = 0
        failures = 0
        for _ in range(max(0, limit)):
            attempt = self.store.claim_next_processing_retry(run_id=run_id)
            if attempt is None:
                break
            attempt_id = int(attempt["id"])
            claim_token = str(attempt["claim_token"])
            completion = "failed"
            error: str | None = "processing retry did not complete"
            try:
                row = self.store.get_message(str(attempt["message_id"]))
                if row is None:
                    completion = "cancelled"
                    error = "message not found"
                elif bool(row["is_deleted"]) or int(row["revision"]) != int(
                    attempt["revision"]
                ):
                    completion = "cancelled"
                    error = "stale message revision"
                else:
                    raw_value = json.loads(row["raw_json"])
                    if not isinstance(raw_value, dict) or not raw_value:
                        raise ValueError("message raw payload is unavailable")
                    message = replace(
                        self.normalizer.normalize(
                            cast(dict[str, Any], raw_value),
                            default_chat_type=row["chat_type"],
                        ),
                        revision=int(row["revision"]),
                        is_deleted=bool(row["is_deleted"]),
                    )
                    now = self.clock()
                    watch_until = _plus_minutes(
                        now, self.config.lifecycle.watch_minutes
                    )
                    source = "p2p" if message.chat_type == "p2p" else "group_at_me"
                    if attempt["stage"] == "task_router":
                        placeholder_reason = (
                            self.store.get_task_router_placeholder_reason(
                                message.message_id, revision=message.revision
                            )
                        )
                        if placeholder_reason is None:
                            raise ValueError(
                                "task-router placeholder routing is unavailable"
                            )
                        rerouted = self.task_processor.run_task_router(
                            message=message,
                            source=source,
                            reason=placeholder_reason,
                            now=now,
                            watch_until=watch_until,
                            run_id=run_id,
                        )
                        if isinstance(rerouted, RoutingResult):
                            self.task_processor.process(
                                message=message,
                                routing=rerouted,
                                source=source,
                                now=now,
                                watch_until=watch_until,
                                run_id=run_id,
                            )
                        status = self.store.message_processing_status(
                            message.message_id,
                            revision=message.revision,
                            stage="task_router",
                        )
                        completion = "succeeded" if status == "processed" else "failed"
                        error = None if completion == "succeeded" else status
                        routed = None
                    else:
                        routed = self.store.get_latest_non_duplicate_routing_decision(
                            message.message_id, revision=message.revision
                        )
                    if attempt["stage"] != "task_router" and routed is None:
                        raise ValueError("routing decision is unavailable")
                    if routed is not None:
                        decision, task = routed
                        if attempt["task_id"] is not None and (
                            task is None or task.id != int(attempt["task_id"])
                        ):
                            raise ValueError("retry task binding is stale")
                        if task is not None and task.status != "watching":
                            completion = "cancelled"
                            error = "task ownership or closure blocks retry"
                        else:
                            if attempt["stage"] == "resource_download":
                                self.resources.process(message, run_id=run_id)
                            self.task_processor.process(
                                message=message,
                                routing=RoutingResult(decision=decision, task=task),
                                source=source,
                                now=now,
                                watch_until=watch_until,
                                run_id=run_id,
                            )
                            status = self.store.message_processing_status(
                                message.message_id,
                                revision=message.revision,
                                stage=str(attempt["stage"]),
                            )
                            completion = (
                                "succeeded" if status == "processed" else "failed"
                            )
                            error = None if completion == "succeeded" else status
                finished = self.store.finish_processing_retry(
                    attempt_id,
                    claim_token=claim_token,
                    status=completion,
                    error=error,
                )
                if not finished:
                    raise RuntimeError("processing retry claim was superseded")
                processed += 1
                failures += completion != "succeeded"
                self.logger.emit(
                    "info" if completion == "succeeded" else "warning",
                    "processing_retry_finished",
                    run_id=run_id,
                    data={
                        "attempt_id": attempt_id,
                        "message_id": attempt["message_id"],
                        "stage": attempt["stage"],
                        "status": completion,
                        "error": error,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self.store.finish_processing_retry(
                    attempt_id,
                    claim_token=claim_token,
                    status="failed",
                    error=str(exc),
                )
                processed += 1
                failures += 1
                self.logger.error(
                    "processing_retry_failed",
                    run_id=run_id,
                    data={
                        "attempt_id": attempt_id,
                        "message_id": attempt["message_id"],
                        "stage": attempt["stage"],
                        "error": str(exc),
                    },
                )
        return StageResult(
            "processing_retries",
            ok=failures == 0,
            processed=processed,
            error=None if failures == 0 else f"{failures} processing retries failed",
        )

    def run_approval_inbox(self, *, run_id: str) -> StageResult:
        if self.approval_service is None:
            return self.run_approval_inbox_placeholder(run_id=run_id)
        bot_open_id = _bot_open_id_from_auth(
            self.feishu_client.auth_status(verify=True).json_data
        )
        if not bot_open_id:
            raise RuntimeError("bot open_id is missing from lark-cli auth status")
        window = self._window("approval_inbox", source="approval_inbox", run_id=run_id)
        drain = self._drain(
            lambda token, page_size, _page_limit: self.feishu_client.list_p2p_messages(
                user_id=bot_open_id,
                start=window.start,
                end=window.end,
                page_token=token,
                page_size=page_size,
            ),
            window=window,
            run_id=run_id,
            source="approval_inbox",
            max_pages=self.config.daemon.ingest_search_max_pages,
            max_messages=self.config.daemon.ingest_search_max_messages,
        )
        drain, batch = self._process_drain_batch(
            window=window,
            drain=drain,
            raws=drain.items,
            source="approval_inbox",
            default_chat_type="p2p",
            run_id=run_id,
        )
        self._record_drain(
            window,
            drain,
            run_id=run_id,
            source="approval_inbox",
            processing_cursor=batch.cursor,
        )
        return StageResult(
            "approval_inbox",
            ok=drain.complete,
            processed=batch.handled_items,
            error=None
            if drain.complete
            else f"approval inbox deferred: {drain.reason}",
        )

    def ingest_group_at_me(self, *, run_id: str) -> StageResult:
        return self._run_search_stage(
            name="group_at_me",
            checkpoint_key="ingest.group_at_me",
            chat_type="group",
            is_at_me=True,
            run_id=run_id,
        )

    def ingest_p2p(self, *, run_id: str) -> StageResult:
        return self._run_search_stage(
            name="p2p",
            checkpoint_key="ingest.p2p",
            chat_type="p2p",
            is_at_me=False,
            run_id=run_id,
        )

    def run_active_watch(self, *, run_id: str) -> StageResult:
        now = self.clock()
        processed = 0
        targets = self.store.list_active_watch_targets(now=now)
        targets = self._rotate_watch_targets(targets)
        self.logger.debug(
            "active_watch_targets_loaded",
            run_id=run_id,
            data={"count": len(targets), "now": now},
        )
        for target in targets:
            chat_id = target["chat_id"]
            thread_id = target["thread_id"]
            if not chat_id:
                self.logger.warning(
                    "active_watch_target_missing_chat_id",
                    run_id=run_id,
                    data={"target": target},
                )
                continue
            if thread_id:
                key = f"active_watch.thread.{thread_id}"
                window = self._window(key, source="active_watch", run_id=run_id)
                drain = self._drain(
                    lambda token, page_size, _page_limit, thread_id=thread_id: (
                        self.feishu_client.list_thread_messages(
                            thread_id=thread_id,
                            page_token=token,
                            page_size=page_size,
                        )
                    ),
                    window=window,
                    run_id=run_id,
                    source="active_watch_thread",
                    max_pages=self.config.daemon.ingest_watch_max_pages_per_target,
                    max_messages=self.config.daemon.ingest_watch_max_messages_per_target,
                )
                raws = _filter_raws_in_window(
                    drain.items, start=window.start, end=window.end
                )
            else:
                key = f"active_watch.chat.{chat_id}"
                window = self._window(key, source="active_watch", run_id=run_id)
                window_start = window.start
                window_end = window.end
                drain = self._drain(
                    lambda token, page_size, _page_limit, chat_id=chat_id, start=window_start, end=window_end: (
                        self.feishu_client.list_chat_messages(
                            chat_id=chat_id,
                            start=start,
                            end=end,
                            page_token=token,
                            page_size=page_size,
                        )
                    ),
                    window=window,
                    run_id=run_id,
                    source="active_watch_chat",
                    max_pages=self.config.daemon.ingest_watch_max_pages_per_target,
                    max_messages=self.config.daemon.ingest_watch_max_messages_per_target,
                )
                raws = self._filter_active_watch_chat_followups(
                    drain.items,
                    default_chat_type=target["chat_type"],
                    now=now,
                )
            drain, batch = self._process_drain_batch(
                window=window,
                drain=drain,
                raws=raws,
                source="active_watch",
                default_chat_type=target["chat_type"],
                run_id=run_id,
            )
            processed += batch.processed
            self._record_drain(
                window,
                drain,
                run_id=run_id,
                source="active_watch_thread" if thread_id else "active_watch_chat",
                processing_cursor=batch.cursor,
            )
            if not drain.complete and drain.reason == "tick_budget_exhausted":
                break
        return StageResult("active_watch", ok=True, processed=processed)

    def _run_search_stage(
        self,
        *,
        name: str,
        checkpoint_key: str,
        chat_type: str,
        is_at_me: bool,
        run_id: str,
    ) -> StageResult:
        window = self._window(checkpoint_key, source=name, run_id=run_id)
        drain = self._drain(
            lambda token, page_size, page_limit: self.feishu_client.search_messages(
                chat_type=chat_type,
                is_at_me=is_at_me,
                start=window.start,
                end=window.end,
                page_token=token,
                query="",
                page_size=page_size,
                page_limit=page_limit,
            ),
            window=window,
            run_id=run_id,
            source=name,
            max_pages=self.config.daemon.ingest_search_max_pages,
            max_messages=self.config.daemon.ingest_search_max_messages,
        )
        drain, batch = self._process_drain_batch(
            window=window,
            drain=drain,
            raws=drain.items,
            source=name,
            default_chat_type=chat_type,
            run_id=run_id,
        )
        self._record_drain(
            window,
            drain,
            run_id=run_id,
            source=name,
            processing_cursor=batch.cursor,
        )
        return StageResult(name, ok=True, processed=batch.processed)

    def _process_raw_batch(
        self,
        raws: list[dict[str, Any]],
        *,
        source: str,
        default_chat_type: str | None,
        run_id: str,
        resume_cursor: ProcessingCursor | None = None,
    ) -> RawBatchResult:
        ordered = sorted(raws, key=_raw_sort_key)
        completed_items = self._validated_processing_prefix(
            ordered,
            resume_cursor=resume_cursor,
            source=source,
            default_chat_type=default_chat_type,
            run_id=run_id,
        )
        processed = 0
        handled_items = 0
        for raw in ordered[completed_items:]:
            if self.monotonic() >= self.ingest_deadline:
                prefix_sha256 = self._processing_prefix_sha256(
                    ordered[:completed_items],
                    default_chat_type=default_chat_type,
                )
                return RawBatchResult(
                    processed=processed,
                    handled_items=handled_items,
                    complete=False,
                    cursor=ProcessingCursor(
                        completed_items=(
                            completed_items if prefix_sha256 is not None else 0
                        ),
                        prefix_sha256=prefix_sha256 or sha256(b"").hexdigest(),
                    ),
                )
            result = self.process_raw_message(
                raw,
                source=source,
                default_chat_type=default_chat_type,
                run_id=run_id,
            )
            completed_items += 1
            handled_items += 1
            if result is not None:
                processed += 1
        return RawBatchResult(
            processed=processed,
            handled_items=handled_items,
            complete=True,
        )

    def _process_drain_batch(
        self,
        *,
        window: DrainWindow,
        drain: DrainResult,
        raws: list[dict[str, Any]],
        source: str,
        default_chat_type: str | None,
        run_id: str,
    ) -> tuple[DrainResult, RawBatchResult]:
        batch = self._process_raw_batch(
            raws,
            source=source,
            default_chat_type=default_chat_type,
            run_id=run_id,
            resume_cursor=self._processing_cursor(window),
        )
        if batch.complete:
            return drain, batch
        # The fetched tail has not completed routing.  Replay this same batch
        # next tick instead of persisting the later token, which would skip it.
        return (
            replace(
                drain,
                complete=False,
                next_page_token=window.page_token,
                reason="tick_budget_exhausted",
                fetch_reason=drain.reason,
            ),
            batch,
        )

    def _processing_cursor(self, window: DrainWindow) -> ProcessingCursor | None:
        backlog_value = window.checkpoint.get("backlog")
        if not isinstance(backlog_value, dict):
            return None
        backlog = cast(dict[str, Any], backlog_value)
        processing_value = backlog.get("processing")
        if not isinstance(processing_value, dict):
            return None
        processing = cast(dict[str, Any], processing_value)
        completed_items = processing.get("completed_items")
        prefix_sha256 = processing.get("prefix_sha256")
        if (
            not isinstance(completed_items, int)
            or completed_items < 0
            or not isinstance(prefix_sha256, str)
            or len(prefix_sha256) != 64
        ):
            return None
        return ProcessingCursor(
            completed_items=completed_items,
            prefix_sha256=prefix_sha256,
        )

    def _validated_processing_prefix(
        self,
        raws: list[dict[str, Any]],
        *,
        resume_cursor: ProcessingCursor | None,
        source: str,
        default_chat_type: str | None,
        run_id: str,
    ) -> int:
        if resume_cursor is None or resume_cursor.completed_items == 0:
            return 0
        if resume_cursor.completed_items <= len(raws):
            prefix = raws[: resume_cursor.completed_items]
            prefix_sha256 = self._processing_prefix_sha256(
                prefix, default_chat_type=default_chat_type
            )
            if (
                prefix_sha256 is not None
                and prefix_sha256 == resume_cursor.prefix_sha256
            ):
                return resume_cursor.completed_items
        self.logger.warning(
            "ingestion_processing_cursor_reset",
            run_id=run_id,
            data={
                "source": source,
                "completed_items": resume_cursor.completed_items,
                "available_items": len(raws),
                "reason": "replayed_prefix_changed",
            },
        )
        return 0

    def _processing_prefix_sha256(
        self,
        raws: list[dict[str, Any]],
        *,
        default_chat_type: str | None,
    ) -> str | None:
        digest = sha256()
        for raw in raws:
            try:
                message = self.normalizer.normalize(
                    raw, default_chat_type=default_chat_type
                )
            except Exception:  # noqa: BLE001
                # A malformed item has no stable normalized identity.  Do not
                # skip an earlier prefix on the next replay.
                return None
            encoded = json.dumps(
                _processing_replay_identity(message),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            digest.update(len(encoded).to_bytes(8, byteorder="big"))
            digest.update(encoded)
        return digest.hexdigest()

    def process_raw_message(
        self,
        raw: dict[str, Any],
        *,
        source: str,
        default_chat_type: str | None,
        run_id: str,
    ) -> RoutingResult | None:
        return self._process_raw_message(
            raw,
            source=source,
            default_chat_type=default_chat_type,
            run_id=run_id,
            enforce_eligibility=True,
        )

    def process_eligible_raw_message(
        self,
        raw: dict[str, Any],
        *,
        source: str,
        default_chat_type: str | None,
        run_id: str,
    ) -> RoutingResult | None:
        """Process a message after Message Eligibility has already passed."""
        return self._process_raw_message(
            raw,
            source=source,
            default_chat_type=default_chat_type,
            run_id=run_id,
            enforce_eligibility=False,
        )

    def _process_raw_message(
        self,
        raw: dict[str, Any],
        *,
        source: str,
        default_chat_type: str | None,
        run_id: str,
        enforce_eligibility: bool,
    ) -> RoutingResult | None:
        try:
            message = self.normalizer.normalize(
                raw, default_chat_type=default_chat_type
            )
        except Exception as exc:
            self.logger.error(
                "message_normalize_failed",
                run_id=run_id,
                data={
                    "raw_message_id": _first_string(
                        raw, "message_id", "messageId", "id"
                    ),
                    "source": source,
                    "default_chat_type": default_chat_type,
                    "error": str(exc),
                },
            )
            raise
        upsert = self.store.upsert_message_with_revision(message)
        message = replace(
            message,
            revision=upsert.revision,
            is_deleted=upsert.is_deleted,
        )
        inserted = upsert.inserted
        self.logger.emit(
            "info",
            "message_ingested",
            run_id=run_id,
            data={
                "message_id": message.message_id,
                "source": source,
                "inserted": inserted,
                "changed": upsert.changed,
                "revision": message.revision,
                "is_deleted": message.is_deleted,
            },
        )
        if message.is_deleted:
            if upsert.changed:
                self.store.invalidate_stale_revision_side_effects(
                    message_id=message.message_id,
                    current_revision=message.revision,
                    reason="source_message_deleted",
                )
                self.store.record_routing_audit(
                    message_id=message.message_id,
                    revision=message.revision,
                    decision=RouteDecision(
                        RouteName.IGNORE, reason="message_tombstone"
                    ),
                )
                self._notify_tombstone_hanging_reply(message)
                self.logger.info(
                    "message_tombstone_recorded",
                    run_id=run_id,
                    data={
                        "message_id": message.message_id,
                        "revision": message.revision,
                    },
                )
            return RoutingResult(
                decision=RouteDecision(RouteName.IGNORE, reason="message_tombstone"),
                task=None,
            )
        if source == "approval_inbox":
            if (
                self.approval_service is not None
                and message.sender_role == "owner_message"
            ):
                result = self.approval_service.apply_command(message=message)
                self.logger.emit(
                    "info",
                    "approval_command_processed",
                    run_id=run_id,
                    data={"message_id": message.message_id, "result": result},
                )
            return None
        if upsert.changed and message.revision > 1:
            self.store.invalidate_stale_revision_side_effects(
                message_id=message.message_id,
                current_revision=message.revision,
                reason="stale_revision",
            )
        now = self.clock()
        watch_until = _plus_minutes(now, self.config.lifecycle.watch_minutes)
        if message.is_self_message:
            sent_action_task = self.store.find_task_for_sent_action_message(
                message.message_id
            )
            if sent_action_task is not None:
                result = self.router.route(
                    message,
                    source=source,
                    inserted=inserted,
                    revision_changed=upsert.changed,
                    now=now,
                    watch_until=watch_until,
                    retry_incomplete_processing=False,
                    agent_working_dir=str(self.agent_working_dir),
                )
                self._log_routing_result(
                    message=message,
                    source=source,
                    inserted=inserted,
                    result=result,
                    run_id=run_id,
                )
                return result
        if enforce_eligibility:
            eligibility = self.eligibility.decide(message, sources=[source])
            if not eligibility.eligible:
                hanging_reply = None
                if upsert.changed and message.revision > 1:
                    hanging_reply = self.store.get_latest_sent_reply_for_source(
                        message_id=message.message_id,
                        before_revision=message.revision,
                    )
                if hanging_reply is None:
                    decision = RouteDecision(
                        RouteName.IGNORE, reason=eligibility.reason_code
                    )
                    self.store.record_routing_audit(
                        message_id=message.message_id,
                        revision=message.revision,
                        decision=decision,
                    )
                    result = RoutingResult(decision=decision, task=None)
                    self._log_routing_result(
                        message=message,
                        source=source,
                        inserted=inserted,
                        result=result,
                        run_id=run_id,
                    )
                    return result
                # A new revision already has a sent-like reply. Eligibility
                # still describes why this would not start work, but dropping
                # here would cancel stale side effects and never offer a
                # correction. Keep routing so revision review can attach.
                self.logger.info(
                    "revision_review_overrides_eligibility",
                    run_id=run_id,
                    data={
                        "message_id": message.message_id,
                        "revision": message.revision,
                        "source": source,
                        "eligibility_reason": eligibility.reason_code,
                        "previous_action_id": hanging_reply.get("action_id"),
                    },
                )
        result = self.router.route(
            message,
            source=source,
            inserted=inserted,
            revision_changed=upsert.changed,
            now=now,
            watch_until=watch_until,
            retry_incomplete_processing=self.task_processor is not None,
            agent_working_dir=str(self.agent_working_dir),
        )
        self._log_routing_result(
            message=message,
            source=source,
            inserted=inserted,
            result=result,
            run_id=run_id,
        )
        if _should_process_resources(
            store=self.store,
            inserted=inserted,
            message=message,
            result=result,
        ):
            self.logger.debug(
                "message_resources_processing_started",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "resource_count": len(message.resources),
                },
            )
            self.resources.process(message, run_id=run_id)
        elif message.resources:
            self.logger.debug(
                "message_resources_processing_skipped",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "resource_count": len(message.resources),
                    "inserted": inserted,
                    "route": result.decision.route,
                },
            )
        if self.task_processor is not None:
            processing = self.task_processor.process(
                message=message,
                routing=result,
                source=source,
                now=now,
                watch_until=watch_until,
                run_id=run_id,
            )
            if processing is not None:
                self.logger.emit(
                    "info",
                    "task_processing_completed",
                    run_id=run_id,
                    data={
                        "message_id": message.message_id,
                        "status": processing.status,
                        "task_id": processing.task_id,
                        "action_id": processing.action_id,
                        "approval_id": processing.approval_id,
                        "reason": processing.reason,
                    },
                )
        return result

    def _notify_tombstone_hanging_reply(self, message: NormalizedMessage) -> None:
        if self.approval_service is None:
            return
        previous = self.store.get_latest_sent_reply_for_source(
            message_id=message.message_id,
            before_revision=message.revision,
        )
        if previous is None:
            return
        task_ids = self.store.find_task_ids_for_message(message.message_id)
        if not task_ids:
            return
        try:
            task = self.store.get_task_by_id(task_ids[-1])
        except KeyError:
            return
        self.approval_service.notify_owner(
            task=task,
            reason="source_recalled_after_send",
            payload={
                "type": "revision_correction_review",
                "message_id": message.message_id,
                "source_message_id": message.message_id,
                "source_revision": message.revision,
                "previous_reply": previous.get("text", ""),
                "previous_action_id": previous.get("action_id"),
                "suggested_reply": "",
                "impact": "high",
                "impact_reasons": ["source_message_deleted"],
                "requires_owner_approval": False,
                "resolution_options": ["send_correction", "no_action"],
                "edit_supported": False,
                "correction_delivery": "explicit_message",
                "commands": [f"/send {task.short_id} <final reply>"],
                "dedupe_key": (
                    f"tombstone-hanging-reply:{message.message_id}:{message.revision}"
                ),
            },
        )

    def _log_routing_result(
        self,
        *,
        message: NormalizedMessage,
        source: str,
        inserted: bool,
        result: RoutingResult,
        run_id: str,
    ) -> None:
        self.logger.info(
            "message_routed",
            run_id=run_id,
            task_id=None if result.task is None else str(result.task.id),
            data={
                "message_id": message.message_id,
                "source": source,
                "route": result.decision.route,
                "reason": result.decision.reason,
                "inserted": inserted,
                "task_short_id": None if result.task is None else result.task.short_id,
            },
        )

    def _window(self, checkpoint_key: str, *, source: str, run_id: str) -> DrainWindow:
        checkpoint = self.store.get_checkpoint(checkpoint_key) or {}
        end = self.clock()
        page_token: str | None = None
        last_success_at = checkpoint.get("last_success_at")
        if isinstance(last_success_at, str):
            start = _minus_seconds(last_success_at, self.config.daemon.overlap_seconds)
        else:
            start = _minus_seconds(end, self.config.daemon.overlap_seconds)
        backlog_value = checkpoint.get("backlog")
        backlog = (
            cast(dict[str, Any], backlog_value)
            if isinstance(backlog_value, dict)
            else None
        )
        if backlog is not None:
            backlog_start = backlog.get("start")
            backlog_end = backlog.get("end")
            backlog_token = backlog.get("next_page_token")
            if isinstance(backlog_start, str) and isinstance(backlog_end, str):
                start = backlog_start
                end = backlog_end
                page_token = backlog_token if isinstance(backlog_token, str) else None
            else:
                backlog = None
        self.logger.debug(
            "ingestion_window_selected",
            run_id=run_id,
            data={
                "source": source,
                "checkpoint_key": checkpoint_key,
                "start": start,
                "end": end,
                "resuming": backlog is not None,
                "has_page_token": page_token is not None,
            },
        )
        return DrainWindow(
            checkpoint_key=checkpoint_key,
            checkpoint=checkpoint,
            start=start,
            end=end,
            page_token=page_token,
        )

    def _record_drain(
        self,
        window: DrainWindow,
        drain: DrainResult,
        *,
        run_id: str,
        source: str,
        processing_cursor: ProcessingCursor | None = None,
    ) -> None:
        previous_backlog_value = window.checkpoint.get("backlog")
        previous_backlog = (
            cast(dict[str, Any], previous_backlog_value)
            if isinstance(previous_backlog_value, dict)
            else None
        )
        previous_pages = (
            int(previous_backlog.get("pages_fetched", 0))
            if previous_backlog is not None
            else 0
        )
        previous_messages = (
            int(previous_backlog.get("messages_fetched", 0))
            if previous_backlog is not None
            else 0
        )
        if drain.complete:
            self.store.set_checkpoint(
                window.checkpoint_key,
                {
                    "last_success_at": window.end,
                    "last_drain": {
                        "pages_fetched": previous_pages + drain.pages,
                        "messages_fetched": previous_messages + len(drain.items),
                        "completed_at": self.clock(),
                    },
                },
            )
            self.logger.emit(
                "info",
                "ingestion_drain_completed",
                run_id=run_id,
                data={
                    "source": source,
                    "checkpoint_key": window.checkpoint_key,
                    "start": window.start,
                    "end": window.end,
                    "pages_fetched": previous_pages + drain.pages,
                    "messages_fetched": previous_messages + len(drain.items),
                    "checkpoint_advanced": True,
                },
            )
            return
        value: dict[str, Any] = {
            key: value
            for key, value in window.checkpoint.items()
            if key not in {"backlog", "last_drain"}
        }
        backlog: dict[str, Any] = {
            "start": window.start,
            "end": window.end,
            "next_page_token": drain.next_page_token,
            "pages_fetched": previous_pages + drain.pages,
            "messages_fetched": previous_messages + len(drain.items),
            "reason": drain.reason,
            "updated_at": self.clock(),
            "restart_count": (
                int(previous_backlog.get("restart_count", 0))
                if previous_backlog is not None
                else 0
            ),
        }
        if drain.fetch_reason is not None:
            backlog["fetch_reason"] = drain.fetch_reason
        if processing_cursor is not None:
            backlog["processing"] = {
                "completed_items": processing_cursor.completed_items,
                "prefix_sha256": processing_cursor.prefix_sha256,
            }
        value["backlog"] = backlog
        self.store.set_checkpoint(window.checkpoint_key, value)
        self.logger.emit(
            "warning",
            "ingestion_drain_deferred",
            run_id=run_id,
            data={
                "source": source,
                "checkpoint_key": window.checkpoint_key,
                "start": window.start,
                "end": window.end,
                "pages_fetched": drain.pages,
                "messages_fetched": len(drain.items),
                "has_next_page_token": drain.next_page_token is not None,
                "reason": drain.reason,
                "fetch_reason": drain.fetch_reason,
                "checkpoint_advanced": False,
                "processing_items_completed": (
                    processing_cursor.completed_items
                    if processing_cursor is not None
                    else None
                ),
                "processing_cursor_saved": processing_cursor is not None,
            },
        )

    def _reset_resumed_token(
        self, window: DrainWindow, *, run_id: str | None, source: str | None
    ) -> None:
        backlog_value = window.checkpoint.get("backlog")
        if not isinstance(backlog_value, dict):
            return
        backlog = cast(dict[str, Any], backlog_value)
        reset_backlog = dict(backlog)
        reset_backlog["next_page_token"] = None
        reset_backlog.pop("processing", None)
        reset_backlog["reason"] = "page_token_reset_after_fetch_failure"
        reset_backlog["updated_at"] = self.clock()
        reset_backlog["restart_count"] = int(backlog.get("restart_count", 0)) + 1
        value = dict(window.checkpoint)
        value["backlog"] = reset_backlog
        self.store.set_checkpoint(window.checkpoint_key, value)
        self.logger.warning(
            "ingestion_resume_token_reset",
            run_id=run_id,
            data={
                "source": source,
                "checkpoint_key": window.checkpoint_key,
                "restart_count": reset_backlog["restart_count"],
            },
        )

    def _rotate_watch_targets(
        self, targets: list[dict[str, str | None]]
    ) -> list[dict[str, str | None]]:
        if not targets:
            return targets
        checkpoint = self.store.get_checkpoint("ingest.scheduler.active_watch") or {}
        start_index = int(checkpoint.get("next_index", 0)) % len(targets)
        self.store.set_checkpoint(
            "ingest.scheduler.active_watch",
            {"next_index": (start_index + 1) % len(targets)},
        )
        return targets[start_index:] + targets[:start_index]

    def ordered_ingestion_stages(self) -> list[Callable[..., StageResult]]:
        stages = [self.ingest_group_at_me, self.ingest_p2p, self.run_active_watch]
        checkpoint = self.store.get_checkpoint("ingest.scheduler.sources") or {}
        start_index = int(checkpoint.get("next_index", 0)) % len(stages)
        self.store.set_checkpoint(
            "ingest.scheduler.sources",
            {"next_index": (start_index + 1) % len(stages)},
        )
        return stages[start_index:] + stages[:start_index]

    def _drain(
        self,
        fetch_page: Callable[[str | None, int, int], MessagePage],
        *,
        window: DrainWindow,
        max_pages: int,
        max_messages: int,
        run_id: str | None = None,
        source: str | None = None,
    ) -> DrainResult:
        items: list[dict[str, Any]] = []
        page_token = window.page_token
        seen_tokens: set[str] = set()
        page_number = 0
        while True:
            if page_number >= max_pages:
                return DrainResult(
                    items, False, page_token, page_number, "page_cap_exhausted"
                )
            if len(items) >= max_messages:
                return DrainResult(
                    items, False, page_token, page_number, "message_cap_exhausted"
                )
            now = self.monotonic()
            if now >= self.ingest_deadline:
                return DrainResult(
                    items, False, page_token, page_number, "tick_budget_exhausted"
                )
            if items and now >= self.ingest_fetch_deadline:
                return DrainResult(
                    items, False, page_token, page_number, "tick_budget_exhausted"
                )
            remaining_messages = max_messages - len(items)
            remaining_pages = max_pages - page_number
            page_size = min(PAGE_SIZE, remaining_messages)
            try:
                page = fetch_page(page_token, page_size, remaining_pages)
            except Exception as exc:
                if page_number == 0 and window.page_token is not None:
                    self._reset_resumed_token(window, run_id=run_id, source=source)
                data: dict[str, Any] = {
                    "source": source,
                    "page_number": page_number + 1,
                    "has_page_token": page_token is not None,
                    "page_token_sha256": _token_fingerprint(page_token),
                    "error": str(exc),
                }
                if isinstance(exc, LarkCliCommandError):
                    result = exc.result
                    data.update(
                        {
                            "cli_exit_code": result.exit_code,
                            "cli_timed_out": result.timed_out,
                            "cli_stderr": result.stderr[:500],
                            "cli_json_error": _json_error_summary(result.json_data),
                            "cli_argv": _redacted_cli_argv(result.argv),
                        }
                    )
                self.logger.error(
                    "message_page_fetch_failed",
                    run_id=run_id,
                    data=data,
                )
                raise
            if len(page.items) > remaining_messages:
                raise RuntimeError(
                    "message page returned "
                    f"{len(page.items)} items above remaining {remaining_messages}"
                )
            if page.page_count < 1 or page.page_count > remaining_pages:
                raise RuntimeError(
                    "message page reported "
                    f"{page.page_count} pages outside remaining {remaining_pages}"
                )
            page_number += page.page_count
            self.logger.debug(
                "message_page_fetched",
                run_id=run_id,
                data={
                    "source": source,
                    "page_number": page_number,
                    "page_count": page.page_count,
                    "items": len(page.items),
                    "has_more": page.has_more,
                    "has_next_page_token": bool(page.next_page_token),
                },
            )
            items.extend(page.items)
            next_token = page.next_page_token
            if not page.has_more or not next_token:
                return DrainResult(items, True, None, page_number)
            if next_token == page_token or next_token in seen_tokens:
                self.logger.error(
                    "message_pagination_token_loop",
                    run_id=run_id,
                    data={"source": source, "page_number": page_number},
                )
                raise RuntimeError(f"pagination token loop detected: {next_token}")
            seen_tokens.add(next_token)
            page_token = next_token

    def _filter_active_watch_chat_followups(
        self,
        raws: list[dict[str, Any]],
        *,
        default_chat_type: str | None,
        now: str,
    ) -> list[dict[str, Any]]:
        if default_chat_type != "group":
            return raws
        return [
            raw
            for raw in raws
            if self._matches_active_watch_key(
                raw, default_chat_type=default_chat_type, now=now
            )
        ]

    def _matches_active_watch_key(
        self,
        raw: dict[str, Any],
        *,
        default_chat_type: str | None,
        now: str,
    ) -> bool:
        message = self.normalizer.normalize(raw, default_chat_type=default_chat_type)
        if not message.chat_id:
            return False
        keys: list[str] = []
        if message.reply_to_message_id:
            keys.append(f"msg:{message.reply_to_message_id}")
        if message.thread_id:
            keys.append(f"thread:{message.thread_id}")
        if message.sender_id:
            keys.append(f"user:{message.sender_id}")
        return any(
            self.store.get_active_tasks_by_watch_key(message.chat_id, key, now=now)
            for key in keys
        )


def _content(raw: dict[str, Any]) -> dict[str, Any]:
    value = raw.get("content")
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"text": value}
        return (
            cast(dict[str, Any], parsed)
            if isinstance(parsed, dict)
            else {"text": value}
        )
    return {}


def _is_deleted(raw: dict[str, Any], content: dict[str, Any]) -> bool:
    """Accept only explicit provider tombstone markers; absence is not deletion."""

    for source in (raw, content):
        for key in ("deleted", "is_deleted", "isDeleted", "recalled", "is_recalled"):
            value = source.get(key)
            if isinstance(value, bool) and value:
                return True
    return False


def _message_text(raw: dict[str, Any], content: dict[str, Any]) -> str:
    for value in (
        raw.get("text"),
        raw.get("message"),
        content.get("text"),
        content.get("title"),
    ):
        if isinstance(value, str):
            return value
    return ""


def _mentions(raw: dict[str, Any], content: dict[str, Any]) -> list[str]:
    mentions: list[str] = []
    for source in (
        raw.get("mentions"),
        raw.get("ats"),
        content.get("mentions"),
        content.get("ats"),
    ):
        if isinstance(source, list):
            for raw_item in cast(list[object], source):
                item = raw_item
                if isinstance(item, str):
                    _append_unique(mentions, item)
                elif isinstance(item, dict):
                    item_map = cast(dict[str, Any], item)
                    value = _first_string(
                        item_map, "open_id", "openId", "user_id", "userId", "id"
                    )
                    if value:
                        _append_unique(mentions, value)
    for user_id in AT_USER_ID_PATTERN.findall(_message_text(raw, content)):
        _append_unique(mentions, user_id)
    return mentions


def _is_at_all(raw: dict[str, Any], text: str, mentions: list[str]) -> bool:
    if raw.get("at_all") is True or raw.get("atAll") is True:
        return True
    lowered = {mention.lower() for mention in mentions}
    lowered_text = text.lower()
    return (
        bool(lowered & {"all", "@all", "@_all", "all_user", "all_users"})
        or "@all" in lowered_text
        or "@_all" in lowered_text
        or "@所有人" in text
    )


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _message_type(raw: dict[str, Any], content: dict[str, Any]) -> str | None:
    return _first_string(
        raw, "msg_type", "msgType", "message_type", "messageType"
    ) or _first_string(content, "msg_type", "msgType", "message_type", "messageType")


def _resources(
    message_id: str, raw: dict[str, Any], content: dict[str, Any]
) -> list[ResourceRef]:
    message_type = _message_type(raw, content)
    if message_type == "merge_forward":
        # Feishu renders forwarded child resources as text placeholders, but the
        # message resource API cannot reliably download them from the container.
        return []
    resources: dict[tuple[str, str], ResourceRef] = {}
    for node in _walk([raw, content]):
        if isinstance(node, dict):
            node_map = cast(dict[str, Any], node)
            image_key = _first_string(node_map, "image_key", "imageKey")
            if image_key:
                resources[("image", image_key)] = ResourceRef(
                    message_id, image_key, "image", node_map
                )
            file_key = _first_string(node_map, "file_key", "fileKey")
            if file_key:
                resources[("file", file_key)] = ResourceRef(
                    message_id, file_key, "file", node_map
                )
        elif isinstance(node, str):
            for image_key in IMAGE_KEY_PATTERN.findall(node):
                resources.setdefault(
                    ("image", image_key),
                    ResourceRef(
                        message_id,
                        image_key,
                        "image",
                        {"source": "text", "file_key": image_key},
                    ),
                )
            for file_key in FILE_KEY_PATTERN.findall(node):
                resources.setdefault(
                    ("file", file_key),
                    ResourceRef(
                        message_id,
                        file_key,
                        "file",
                        {"source": "text", "file_key": file_key},
                    ),
                )
    return list(resources.values())


def _walk(value: Any) -> list[Any]:
    items = [value]
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        for child in mapping.values():
            items.extend(_walk(child))
    elif isinstance(value, list):
        for child in cast(list[object], value):
            items.extend(_walk(child))
    return items


def _thread_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("thread_id") or raw.get("threadId") or raw.get("thread")
    if isinstance(value, dict):
        return _first_string(cast(dict[str, Any], value), "id", "thread_id", "threadId")
    return str(value) if value else None


def _bot_open_id_from_auth(auth_json: Any) -> str | None:
    if not isinstance(auth_json, dict):
        return None
    auth_map = cast(dict[str, Any], auth_json)
    identities = auth_map.get("identities")
    if not isinstance(identities, dict):
        return None
    identities_map = cast(dict[str, Any], identities)
    bot = identities_map.get("bot")
    if not isinstance(bot, dict):
        return None
    return _first_string(cast(dict[str, Any], bot), "openId", "open_id", "openID", "id")


def _first_string(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _raw_sort_key(raw: dict[str, Any]) -> tuple[float, str]:
    sent_at = _first_string(raw, "create_time", "created_at", "sent_at", "timestamp")
    parsed = _parse_dt_or_none(sent_at) if sent_at is not None else None
    return (
        parsed.timestamp() if parsed is not None else float("-inf"),
        _first_string(raw, "message_id", "messageId", "id") or "",
    )


def _processing_replay_identity(message: NormalizedMessage) -> dict[str, Any]:
    """Return exactly the normalized fields that can affect replay behavior."""
    return {
        "message_id": message.message_id,
        "sent_at": message.sent_at,
        "chat_id": message.chat_id,
        "chat_type": message.chat_type,
        "sender_id": message.sender_id,
        "sender_type": message.sender_type,
        "sender_role": message.sender_role,
        "thread_id": message.thread_id,
        "reply_to_message_id": message.reply_to_message_id,
        "text": message.text,
        "direct_mention": message.direct_mention,
        "at_all": message.at_all,
        "mentions": sorted(message.mentions),
        "message_type": message.message_type,
        "resources": sorted(
            (resource.file_key, resource.resource_type)
            for resource in message.resources
        ),
        "is_deleted": message.is_deleted,
    }


def _filter_raws_in_window(
    raws: list[dict[str, Any]], *, start: str, end: str
) -> list[dict[str, Any]]:
    start_dt = parse_instant(start)
    end_dt = parse_instant(end)
    filtered: list[dict[str, Any]] = []
    for raw in raws:
        sent_at = _first_string(
            raw, "create_time", "created_at", "sent_at", "timestamp"
        )
        sent_dt = _parse_dt_or_none(sent_at) if sent_at is not None else None
        if sent_dt is None or start_dt <= sent_dt <= end_dt:
            filtered.append(raw)
    return filtered


def _should_process_resources(
    *,
    store: SQLiteStore,
    inserted: bool,
    message: NormalizedMessage,
    result: RoutingResult,
) -> bool:
    if not message.resources:
        return False
    if (
        message.is_self_message
        or message.sender_role == "owner_message"
        or message.at_all
    ):
        return False
    eligible_routes = {"new_task", "attach_task", "reopen_task", "ambiguous"}
    if result.decision.route in eligible_routes:
        return True
    return (
        not inserted
        and result.decision.reason == "duplicate_message"
        and store.has_resource_eligible_routing_audit(
            message.message_id, revision=message.revision
        )
        and store.has_missing_resources(message.resources)
    )


def _minus_seconds(value: str, seconds: int) -> str:
    return shift_instant(value, delta=-timedelta(seconds=seconds))


def _plus_minutes(value: str, minutes: int) -> str:
    return shift_instant(value, delta=timedelta(minutes=minutes))


def _parse_dt_or_none(value: str) -> datetime | None:
    try:
        normalized = normalize_message_sent_at(value)
    except ValueError:
        return None
    return parse_instant_or_none(normalized)


def _resource_output(resource: ResourceRef, resource_dir: str) -> str:
    message_part = _safe_path_part(resource.message_id)
    key_hash = sha256(resource.file_key.encode("utf-8")).hexdigest()[:12]
    return PurePosixPath(
        resource_dir, message_part, f"{resource.resource_type}_{key_hash}.bin"
    ).as_posix()


def _safe_path_part(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"-", "_"} else "_" for char in value
    )[:120]


def _sha256_if_exists(path: str | Path) -> str | None:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        return None
    digest = sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_download(temporary_path: Path, final_path: Path) -> None:
    """Durably publish a validated download without exposing partial contents."""
    with temporary_path.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary_path, final_path)
    try:
        directory_fd = os.open(final_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        with suppress(OSError):
            # Some filesystems do not support directory fsync; the atomic
            # replace has still completed and the validated file is usable.
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _normalized_download_result(
    result: Any,
    *,
    temporary_output: str,
    final_output: str,
) -> Any:
    if not isinstance(result, dict):
        return result
    result_map = cast(dict[str, Any], result)
    if result_map.get("output") != temporary_output:
        return result_map
    return result_map | {"output": final_output}
