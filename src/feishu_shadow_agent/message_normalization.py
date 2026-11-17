from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from .time_utils import format_instant, normalize_instant
from .types import NormalizedMessage, ResourceRef, SenderRole

FEISHU_MESSAGE_TIMEZONE = ZoneInfo("Asia/Shanghai")
IMAGE_KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(img_[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)
FILE_KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(file_[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)
FOLDER_KEY_PATTERN = re.compile(
    r"<folder\b[^>]*\bkey=[\"'](file_[A-Za-z0-9_-]+)[\"'][^>]*>",
    re.IGNORECASE,
)
AT_USER_ID_PATTERN = re.compile(
    r"<at\s+[^>]*user_id=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE
)


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
        message_id = first_string(raw, "message_id", "messageId", "id") or ""
        if not message_id:
            raise ValueError("message is missing message_id")
        content = _content(raw)
        sender_value = raw.get("sender")
        sender = (
            cast(dict[str, Any], sender_value) if isinstance(sender_value, dict) else {}
        )
        sender_id = first_string(
            raw, "sender_id", "senderId", "open_id", "openId"
        ) or first_string(sender, "sender_id", "senderId", "open_id", "openId", "id")
        profile_value = sender.get("profile")
        profile = (
            cast(dict[str, Any], profile_value)
            if isinstance(profile_value, dict)
            else {}
        )
        sender_name = (
            first_string(
                raw, "sender_name", "senderName", "user_name", "userName", "name"
            )
            or first_string(
                sender, "sender_name", "senderName", "user_name", "userName", "name"
            )
            or first_string(
                profile,
                "name",
                "display_name",
                "displayName",
            )
            or sender_id
        )
        sender_type = first_string(raw, "sender_type", "senderType") or first_string(
            sender, "sender_type", "senderType", "type"
        )
        chat_value = raw.get("chat")
        chat = cast(dict[str, Any], chat_value) if isinstance(chat_value, dict) else {}
        chat_id = first_string(raw, "chat_id", "chatId") or first_string(
            chat, "chat_id", "chatId", "id"
        )
        chat_type = first_string(raw, "chat_type", "chatType") or default_chat_type
        if chat_type not in {"group", "p2p"}:
            chat_type = None
        sent_at = normalize_message_sent_at(
            first_string(raw, "create_time", "created_at", "sent_at", "timestamp")
        )
        thread_id = _thread_id(raw)
        reply_to_value = raw.get("reply_to") or raw.get("replyTo")
        reply_to = (
            cast(dict[str, Any], reply_to_value)
            if isinstance(reply_to_value, dict)
            else {}
        )
        reply_to_message_id = first_string(
            raw,
            "reply_to_message_id",
            "replyToMessageId",
            "reply_to",
            "replyTo",
            "parent_id",
            "parentId",
            "root_id",
            "rootId",
        ) or first_string(
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
                    value = first_string(
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
    return first_string(
        raw, "msg_type", "msgType", "message_type", "messageType"
    ) or first_string(content, "msg_type", "msgType", "message_type", "messageType")


def _resources(
    message_id: str, raw: dict[str, Any], content: dict[str, Any]
) -> list[ResourceRef]:
    folder_keys = _folder_keys(raw, content)
    resources: dict[tuple[str, str], ResourceRef] = {}
    for node in _walk([raw, content]):
        if isinstance(node, dict):
            node_map = cast(dict[str, Any], node)
            image_key = first_string(node_map, "image_key", "imageKey")
            if image_key:
                resources[("image", image_key)] = ResourceRef(
                    message_id, image_key, "image", node_map
                )
            file_key = first_string(node_map, "file_key", "fileKey")
            if file_key and file_key not in folder_keys and not _is_folder(node_map):
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
                if file_key in folder_keys:
                    continue
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


def _folder_keys(raw: dict[str, Any], content: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for node in _walk([raw, content]):
        if isinstance(node, str):
            keys.update(FOLDER_KEY_PATTERN.findall(node))
        elif isinstance(node, dict):
            node_map = cast(dict[str, Any], node)
            file_key = first_string(node_map, "file_key", "fileKey")
            if file_key and _is_folder(node_map):
                keys.add(file_key)
    return keys


def _is_folder(value: dict[str, Any]) -> bool:
    return value.get("is_folder") is True or value.get("isFolder") is True


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
        return first_string(cast(dict[str, Any], value), "id", "thread_id", "threadId")
    return str(value) if value else None


def first_string(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return None
