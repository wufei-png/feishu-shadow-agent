from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

ReplyLocation = Literal["main", "thread"]
POSITION_FIELDS = ("message_position", "thread_message_position")


@dataclass(frozen=True)
class ReplyCandidate:
    message_id: str
    chat_id: str
    locations: tuple[ReplyLocation, ...]
    thread_id: str | None = None

    def prompt_value(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "message_id": self.message_id,
            "locations": list(self.locations),
        }
        if self.thread_id:
            result["thread_id"] = self.thread_id
        return result

    def destination(self, location: str | None) -> dict[str, str] | None:
        if location is None and len(self.locations) == 1:
            location = self.locations[0]
        if location is None or location not in self.locations:
            return None
        result = {"chat_id": self.chat_id, "location": location}
        if location == "thread":
            if not self.thread_id:
                return None
            result["thread_id"] = self.thread_id
        return result


def candidate_from_message(
    row: Mapping[str, Any],
    *,
    expected_chat_id: str | None,
    chat_type: str | None,
    allow_deleted: bool = False,
) -> ReplyCandidate | None:
    message_id = row.get("message_id")
    chat_id = row.get("chat_id")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(chat_id, str)
        or not chat_id
        or not expected_chat_id
        or chat_id != expected_chat_id
        or (row.get("is_deleted") and not allow_deleted)
    ):
        return None
    raw = _raw_message(row)
    thread_id = row.get("thread_id") or raw.get("thread_id")
    thread_id = thread_id if isinstance(thread_id, str) and thread_id else None
    main_position = position_value(raw.get("message_position"))
    thread_position = position_value(raw.get("thread_message_position"))
    if raw.get("reply_position_conflict") is True or has_invalid_position(raw):
        locations: tuple[ReplyLocation, ...] = ()
    elif chat_type == "p2p" and thread_id is None:
        locations = ("main",)
    elif chat_type != "group":
        return None
    elif (
        thread_position is not None
        and thread_position >= 0
        and main_position is not None
        and main_position >= 0
    ):
        locations = ()
    elif thread_position is not None and thread_position >= 0:
        locations = ("thread",) if thread_id else ()
    elif main_position is not None and main_position >= 0:
        locations = ("main", "thread") if thread_id else ("main",)
    else:
        # A thread ID can also belong to a root message in the main chat; it
        # is not by itself evidence of which container contains the message.
        locations = ()
    return ReplyCandidate(message_id, chat_id, locations, thread_id)


def message_location(raw: Mapping[str, Any], *, chat_type: str | None) -> str | None:
    if has_invalid_position(raw):
        return None
    thread_position = position_value(raw.get("thread_message_position"))
    main_position = position_value(raw.get("message_position"))
    if (
        thread_position is not None
        and thread_position >= 0
        and main_position is not None
        and main_position >= 0
    ):
        return None
    if thread_position is not None and thread_position >= 0:
        return "thread"
    if main_position is not None and main_position >= 0:
        return "main"
    if chat_type == "p2p" and not raw.get("thread_id"):
        return "main"
    return None


def _raw_message(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("raw_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    if not isinstance(value, dict):
        return {}
    return cast(dict[str, Any], value)


def has_invalid_position(raw: Mapping[str, Any]) -> bool:
    return any(
        raw.get(key) is not None and position_value(raw[key]) is None
        for key in POSITION_FIELDS
    )


def position_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if isinstance(value, (int, str)) else None
    except ValueError:
        return None
