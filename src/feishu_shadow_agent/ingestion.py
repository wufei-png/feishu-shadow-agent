from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .config import AppConfig, ChatPolicyConfig
from .feishu.client import FeishuClient
from .jsonl import JSONLLogger
from .processing import ApprovalService, TaskProcessingService
from .routing import MessageRouter, RoutingResult
from .store.sqlite_store import SQLiteStore
from .types import MessagePage, NormalizedMessage, ResourceRef, utc_now_iso

PAGE_SIZE = 50
WATCH_EXTEND_MINUTES = 120
IMAGE_KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])(img_[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])")
FILE_KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])(file_[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])")
AT_USER_ID_PATTERN = re.compile(r"<at\s+[^>]*user_id=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class StageResult:
    name: str
    ok: bool
    processed: int = 0
    error: str | None = None


class MessageNormalizer:
    def __init__(self, *, owner_open_id: str):
        self.owner_open_id = owner_open_id

    def normalize(self, raw: dict[str, Any], *, default_chat_type: str | None = None) -> NormalizedMessage:
        message_id = _first_string(raw, "message_id", "messageId", "id") or ""
        if not message_id:
            raise ValueError("message is missing message_id")
        content = _content(raw)
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        sender_id = (
            _first_string(raw, "sender_id", "senderId", "open_id", "openId")
            or _first_string(sender, "sender_id", "senderId", "open_id", "openId", "id")
        )
        sender_name = (
            _first_string(raw, "sender_name", "senderName", "user_name", "userName", "name")
            or _first_string(sender, "sender_name", "senderName", "user_name", "userName", "name")
            or _first_string(
                sender.get("profile") if isinstance(sender.get("profile"), dict) else {},
                "name",
                "display_name",
                "displayName",
            )
            or sender_id
        )
        sender_type = (
            _first_string(raw, "sender_type", "senderType")
            or _first_string(sender, "sender_type", "senderType", "type")
        )
        chat = raw.get("chat") if isinstance(raw.get("chat"), dict) else {}
        chat_id = (
            _first_string(raw, "chat_id", "chatId")
            or _first_string(chat, "chat_id", "chatId", "id")
        )
        chat_type = _first_string(raw, "chat_type", "chatType") or default_chat_type
        if chat_type not in {"group", "p2p"}:
            chat_type = None
        sent_at = _first_string(raw, "create_time", "created_at", "sent_at", "timestamp")
        thread_id = _thread_id(raw)
        reply_to_value = raw.get("reply_to") or raw.get("replyTo")
        reply_to = reply_to_value if isinstance(reply_to_value, dict) else {}
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
        direct_mention = bool(raw.get("is_at_me") or raw.get("isAtMe") or self.owner_open_id in mentions)
        if at_all:
            direct_mention = False
        return NormalizedMessage(
            message_id=message_id,
            chat_id=chat_id,
            chat_type=chat_type,  # type: ignore[arg-type]
            sender_id=sender_id,
            sender_name=sender_name,
            sender_type=sender_type,
            sender_role=self._sender_role(sender_id=sender_id, sender_type=sender_type, raw=raw),
            sent_at=sent_at,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            text=text,
            direct_mention=direct_mention,
            at_all=at_all,
            mentions=mentions,
            resources=_resources(message_id, raw, content),
            raw=raw,
        )

    def _sender_role(self, *, sender_id: str | None, sender_type: str | None, raw: dict[str, Any]) -> str:
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
        feishu_client: FeishuClient,
        config: AppConfig,
        logger: JSONLLogger,
        config_base_dir: str | Path | None = None,
    ):
        self.store = store
        self.feishu_client = feishu_client
        self.config = config
        self.logger = logger
        self.config_base_dir = Path(config_base_dir or Path.cwd()).expanduser().resolve()

    def process(self, message: NormalizedMessage, *, run_id: str | None = None) -> None:
        if not message.resources:
            return
        policy = self._chat_policy(message.chat_id)
        for resource in message.resources:
            if not policy.resource_download:
                self.store.upsert_resource(resource, download_status="skipped")
                continue
            if not policy.bot_joined:
                self.store.upsert_resource(
                    resource,
                    download_status="bot_not_joined",
                    raw={"reason": "chat_policy_bot_joined_false"},
                )
                continue
            output = _resource_output(resource, self.config.storage.resource_dir)
            local_output = self.config_base_dir / output
            try:
                local_output.parent.mkdir(parents=True, exist_ok=True)
                result = self.feishu_client.download_resource(
                    message_id=resource.message_id,
                    file_key=resource.file_key,
                    resource_type=resource.resource_type,
                    output=output,
                )
            except Exception as exc:
                self.store.upsert_resource(
                    resource,
                    download_status="failed",
                    path=output,
                    raw={"error": str(exc)},
                )
                self.logger.emit(
                    "warning",
                    "resource_download_failed",
                    run_id=run_id,
                    data={"message_id": resource.message_id, "file_key": resource.file_key, "error": str(exc)},
                )
                continue
            if result.ok:
                sha256_hex = _sha256_if_exists(local_output)
                if sha256_hex is None:
                    self.store.upsert_resource(
                        resource,
                        download_status="missing_file",
                        path=output,
                        raw={"result": result.json_data, "error": "download output file missing"},
                    )
                    self.logger.emit(
                        "warning",
                        "resource_download_missing_file",
                        run_id=run_id,
                        data={"message_id": resource.message_id, "file_key": resource.file_key, "path": output},
                    )
                    continue
                self.store.upsert_resource(
                    resource,
                    download_status="downloaded",
                    path=output,
                    sha256_hex=sha256_hex,
                    raw={"result": result.json_data},
                )
            else:
                status = "bot_invisible" if _bot_invisible_error(result) else "failed"
                self.store.upsert_resource(
                    resource,
                    download_status=status,
                    path=output,
                    raw={"error": result.error, "stderr": result.stderr, "stdout": result.stdout},
                )

    def _chat_policy(self, chat_id: str | None) -> ChatPolicyConfig:
        if chat_id and chat_id in self.config.chats:
            return self.config.chats[chat_id]
        return ChatPolicyConfig()


class IngestionService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        feishu_client: FeishuClient,
        config: AppConfig,
        logger: JSONLLogger,
        router: MessageRouter | None = None,
        task_processor: TaskProcessingService | None = None,
        approval_service: ApprovalService | None = None,
        clock: Callable[[], str] = utc_now_iso,
        config_base_dir: str | Path | None = None,
    ):
        self.store = store
        self.feishu_client = feishu_client
        self.config = config
        self.logger = logger
        self.normalizer = MessageNormalizer(owner_open_id=config.owner.open_id)
        self.router = router or MessageRouter(store=store)
        self.task_processor = task_processor
        self.approval_service = approval_service or (task_processor.approvals if task_processor is not None else None)
        self.resources = ResourceProcessor(
            store=store,
            feishu_client=feishu_client,
            config=config,
            logger=logger,
            config_base_dir=config_base_dir,
        )
        self.clock = clock

    def run_approval_inbox_placeholder(self, *, run_id: str) -> StageResult:
        self.logger.emit(
            "info",
            "approval_inbox_placeholder",
            run_id=run_id,
            data={"checkpoint": "approval_inbox", "checkpoint_written": False},
        )
        return StageResult("approval_inbox", ok=True)

    def run_approval_inbox(self, *, run_id: str) -> StageResult:
        if self.approval_service is None:
            return self.run_approval_inbox_placeholder(run_id=run_id)
        bot_open_id = _bot_open_id_from_auth(self.feishu_client.auth_status(verify=True).json_data)
        if not bot_open_id:
            raise RuntimeError("bot open_id is missing from lark-cli auth status")
        start, end = self._window("approval_inbox")
        raws = self._drain(lambda token: self.feishu_client.list_p2p_messages(
            user_id=bot_open_id,
            start=start,
            end=end,
            page_token=token,
            page_size=PAGE_SIZE,
        ))
        self._process_raw_batch(
            raws,
            source="approval_inbox",
            default_chat_type="p2p",
            run_id=run_id,
        )
        self.store.set_checkpoint("approval_inbox", {"last_success_at": end})
        return StageResult("approval_inbox", ok=True, processed=len(raws))

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
        for target in self.store.list_active_watch_targets(now=now):
            chat_id = target["chat_id"]
            thread_id = target["thread_id"]
            if not chat_id:
                continue
            if thread_id:
                key = f"active_watch.thread.{thread_id}"
                start, end = self._window(key)
                raws = self._drain(lambda token: self.feishu_client.list_thread_messages(
                    thread_id=thread_id,
                    page_token=token,
                    page_size=PAGE_SIZE,
                ))
                raws = _filter_raws_in_window(raws, start=start, end=end)
            else:
                key = f"active_watch.chat.{chat_id}"
                start, end = self._window(key)
                raws = self._drain(lambda token: self.feishu_client.list_chat_messages(
                    chat_id=chat_id,
                    start=start,
                    end=end,
                    page_token=token,
                    page_size=PAGE_SIZE,
                ))
                raws = self._filter_active_watch_chat_followups(
                    raws,
                    default_chat_type=target["chat_type"],
                    now=now,
                )
            processed += self._process_raw_batch(
                raws,
                source="active_watch",
                default_chat_type=target["chat_type"],
                run_id=run_id,
            )
            self.store.set_checkpoint(key, {"last_success_at": end})
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
        start, end = self._window(checkpoint_key)
        raws = self._drain(lambda token: self.feishu_client.search_messages(
            chat_type=chat_type,
            is_at_me=is_at_me,
            start=start,
            end=end,
            page_token=token,
            query="",
            page_size=PAGE_SIZE,
        ))
        processed = self._process_raw_batch(
            raws,
            source=name,
            default_chat_type=chat_type,
            run_id=run_id,
        )
        self.store.set_checkpoint(checkpoint_key, {"last_success_at": end})
        return StageResult(name, ok=True, processed=processed)

    def _process_raw_batch(
        self,
        raws: list[dict[str, Any]],
        *,
        source: str,
        default_chat_type: str | None,
        run_id: str,
    ) -> int:
        processed = 0
        for raw in sorted(raws, key=_raw_sort_key):
            result = self.process_raw_message(
                raw,
                source=source,
                default_chat_type=default_chat_type,
                run_id=run_id,
            )
            if result is not None:
                processed += 1
        return processed

    def process_raw_message(
        self,
        raw: dict[str, Any],
        *,
        source: str,
        default_chat_type: str | None,
        run_id: str,
    ) -> RoutingResult | None:
        message = self.normalizer.normalize(raw, default_chat_type=default_chat_type)
        inserted = self.store.upsert_message(message)
        self.logger.emit(
            "info",
            "message_ingested",
            run_id=run_id,
            data={"message_id": message.message_id, "source": source, "inserted": inserted},
        )
        if source == "approval_inbox":
            if self.approval_service is not None and message.sender_role == "owner_message":
                result = self.approval_service.apply_command(message=message)
                self.logger.emit(
                    "info",
                    "approval_command_processed",
                    run_id=run_id,
                    data={"message_id": message.message_id, "result": result},
                )
            return None
        now = self.clock()
        watch_until = _plus_minutes(now, WATCH_EXTEND_MINUTES)
        result = self.router.route(
            message,
            source=source,
            inserted=inserted,
            now=now,
            watch_until=watch_until,
        )
        if _should_process_resources(
            store=self.store,
            inserted=inserted,
            message=message,
            result=result,
        ):
            self.resources.process(message, run_id=run_id)
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

    def _window(self, checkpoint_key: str) -> tuple[str, str]:
        end = self.clock()
        checkpoint = self.store.get_checkpoint(checkpoint_key) or {}
        last_success_at = checkpoint.get("last_success_at")
        if isinstance(last_success_at, str):
            start = _minus_seconds(last_success_at, self.config.daemon.overlap_seconds)
        else:
            start = _minus_seconds(end, self.config.daemon.overlap_seconds)
        return start, end

    def _drain(self, fetch_page: Callable[[str | None], MessagePage]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            page = fetch_page(page_token)
            items.extend(page.items)
            next_token = page.next_page_token
            if not page.has_more or not next_token:
                return items
            if next_token in seen_tokens:
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
            if self._matches_active_watch_key(raw, default_chat_type=default_chat_type, now=now)
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
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"text": value}
        return parsed if isinstance(parsed, dict) else {"text": value}
    return {}


def _message_text(raw: dict[str, Any], content: dict[str, Any]) -> str:
    for value in (raw.get("text"), raw.get("message"), content.get("text"), content.get("title")):
        if isinstance(value, str):
            return value
    return ""


def _mentions(raw: dict[str, Any], content: dict[str, Any]) -> list[str]:
    mentions: list[str] = []
    for source in (raw.get("mentions"), raw.get("ats"), content.get("mentions"), content.get("ats")):
        if isinstance(source, list):
            for item in source:
                if isinstance(item, str):
                    _append_unique(mentions, item)
                elif isinstance(item, dict):
                    value = _first_string(item, "open_id", "openId", "user_id", "userId", "id")
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


def _resources(message_id: str, raw: dict[str, Any], content: dict[str, Any]) -> list[ResourceRef]:
    resources: dict[tuple[str, str], ResourceRef] = {}
    for node in _walk([raw, content]):
        if isinstance(node, dict):
            image_key = _first_string(node, "image_key", "imageKey")
            if image_key:
                resources[("image", image_key)] = ResourceRef(message_id, image_key, "image", node)
            file_key = _first_string(node, "file_key", "fileKey")
            if file_key:
                resources[("file", file_key)] = ResourceRef(message_id, file_key, "file", node)
        elif isinstance(node, str):
            for image_key in IMAGE_KEY_PATTERN.findall(node):
                resources.setdefault(
                    ("image", image_key),
                    ResourceRef(message_id, image_key, "image", {"source": "text", "file_key": image_key}),
                )
            for file_key in FILE_KEY_PATTERN.findall(node):
                resources.setdefault(
                    ("file", file_key),
                    ResourceRef(message_id, file_key, "file", {"source": "text", "file_key": file_key}),
                )
    return list(resources.values())


def _walk(value: Any) -> list[Any]:
    items = [value]
    if isinstance(value, dict):
        for child in value.values():
            items.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk(child))
    return items


def _thread_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("thread_id") or raw.get("threadId") or raw.get("thread")
    if isinstance(value, dict):
        return _first_string(value, "id", "thread_id", "threadId")
    return str(value) if value else None


def _bot_open_id_from_auth(auth_json: Any) -> str | None:
    if not isinstance(auth_json, dict):
        return None
    identities = auth_json.get("identities")
    if not isinstance(identities, dict):
        return None
    bot = identities.get("bot")
    if not isinstance(bot, dict):
        return None
    return _first_string(bot, "openId", "open_id", "openID", "id")


def _first_string(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _raw_sort_key(raw: dict[str, Any]) -> tuple[str, str]:
    return (
        _first_string(raw, "create_time", "created_at", "sent_at", "timestamp") or "",
        _first_string(raw, "message_id", "messageId", "id") or "",
    )


def _filter_raws_in_window(raws: list[dict[str, Any]], *, start: str, end: str) -> list[dict[str, Any]]:
    start_dt = _parse_dt_or_none(start)
    end_dt = _parse_dt_or_none(end)
    if start_dt is None or end_dt is None:
        return raws
    filtered: list[dict[str, Any]] = []
    for raw in raws:
        sent_at = _first_string(raw, "create_time", "created_at", "sent_at", "timestamp")
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
    if message.is_self_message or message.sender_role == "owner_message" or message.at_all:
        return False
    eligible_routes = {"new_task", "attach_task", "reopen_task", "ambiguous"}
    if result.decision.route in eligible_routes:
        return True
    return (
        not inserted
        and result.decision.reason == "duplicate_message"
        and store.has_resource_eligible_routing_audit(message.message_id)
        and store.has_missing_resources(message.resources)
    )


def _minus_seconds(value: str, seconds: int) -> str:
    return (_parse_dt(value) - timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")


def _plus_minutes(value: str, minutes: int) -> str:
    return (_parse_dt(value) + timedelta(minutes=minutes)).astimezone().isoformat(timespec="seconds")


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now().astimezone()


def _parse_dt_or_none(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def _resource_output(resource: ResourceRef, resource_dir: str) -> str:
    message_part = _safe_path_part(resource.message_id)
    key_hash = sha256(resource.file_key.encode("utf-8")).hexdigest()[:12]
    return PurePosixPath(resource_dir, message_part, f"{resource.resource_type}_{key_hash}.bin").as_posix()


def _safe_path_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)[:120]


def _sha256_if_exists(path: str | Path) -> str | None:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        return None
    digest = sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bot_invisible_error(result: Any) -> bool:
    text = " ".join(str(part) for part in (getattr(result, "error", ""), getattr(result, "stderr", "")))
    return "234002" in text or "234040" in text or "invisible" in text.lower()
