from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .types import NormalizedMessage, TaskRecord

WATCH_EXTEND_MINUTES = 120

ROUTER_INSTRUCTION = (
    "Route the incoming Feishu message. Return one strict JSON object that conforms to output_schema. "
    "Do not include Markdown or explanatory text."
)
TASK_SESSION_INSTRUCTION = (
    "Handle this Feishu task. Return one strict JSON object that conforms to output_schema. "
    "Do not include Markdown, explanatory text, or @ mentions."
)


class StrictModel(BaseModel):
    # Agent output is an API boundary. Unknown fields are rejected so prompt
    # drift becomes an auditable owner path instead of silently changing policy.
    model_config = ConfigDict(extra="forbid")


class TaskRouterOutput(StrictModel):
    route: Literal["new_task", "attach_task", "reopen_task", "close_task", "ignore", "ambiguous"] = Field(
        description="Routing decision for the incoming message."
    )
    target_task_id: str | None = Field(default=None, description="Task short id to target, or null when no target applies.")
    confidence: float = Field(ge=0, le=1, description="Confidence in the routing decision, from 0 to 1.")
    reason: str = Field(default="", description="Short operator-readable reason for the decision.")
    updated_watch_keys: list[str] = Field(
        default_factory=list,
        description="Task watch keys to add, formatted as user:<id>, msg:<id>, or thread:<id>.",
    )


class TaskSessionOutput(StrictModel):
    task_label: str = Field(description="Short task label for operator status views.")
    task_state: Literal["needs_reply", "watching", "closed", "waiting"] = Field(
        description="Current task state after handling the message."
    )
    answerability: Literal["auto_reply", "needs_owner", "no_reply"] = Field(
        description="Whether the daemon may reply automatically, needs owner review, or should not reply."
    )
    confidence: float = Field(ge=0, le=1, description="Confidence in the proposed action, from 0 to 1.")
    proposed_reply: str = Field(default="", description="Plain reply text without Feishu @ mentions.")
    reply_target_message_id: str | None = Field(
        default=None,
        description="Message id to reply to; must be one of metadata.reply_target_message_ids.",
    )
    watch_action: Literal["keep_watching", "close"] = Field(
        default="keep_watching",
        description="Whether to keep watching this task or close it.",
    )
    watch_extend_minutes: int = Field(
        default=WATCH_EXTEND_MINUTES,
        ge=0,
        le=24 * 60,
        description="Minutes to extend task watching when watch_action keeps watching.",
    )
    risk_level: Literal["low", "medium", "high"] = Field(
        default="low",
        description="Risk level of sending the proposed reply.",
    )
    safety_notes: list[str] = Field(default_factory=list, description="Short safety or uncertainty notes.")
    requires_resources: bool = Field(
        default=False,
        description="True when the answer depends on downloaded image/file resources.",
    )

    @field_validator("task_label")
    @classmethod
    def trim_label(cls, value: str) -> str:
        return " ".join(value.split())[:100]


def build_router_prompt(*, message: NormalizedMessage, active: list[Any], historical: list[TaskRecord]) -> str:
    payload = {
        "instruction": ROUTER_INSTRUCTION,
        "output_schema": _schema_hint(TaskRouterOutput),
        "message": _message_card(message),
        "active_candidates": [_candidate_card(candidate.task, candidate.matched_by) for candidate in active],
        "historical_candidates": [_task_card(task) for task in historical],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_task_session_prompt(
    *,
    task: TaskRecord,
    current_message_id: str,
    reply_target_message_ids: list[str],
    messages: list[Any],
    resources: list[Any],
) -> str:
    payload = {
        "instruction": TASK_SESSION_INSTRUCTION,
        "metadata": {
            "current_message_id": current_message_id,
            "root_message_id": task.root_message_id,
            "reply_target_message_ids": reply_target_message_ids,
        },
        "output_schema": _schema_hint(TaskSessionOutput),
        "task": _task_card(task),
        "messages": [_row_message_card(row) for row in messages],
        "resources": [_resource_card(row) for row in resources],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _schema_hint(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def _message_card(message: NormalizedMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "chat_id": message.chat_id,
        "chat_type": message.chat_type,
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "sent_at": message.sent_at,
        "thread_id": message.thread_id,
        "reply_to_message_id": message.reply_to_message_id,
        "text": message.text,
        "direct_mention": message.direct_mention,
    }


def _row_message_card(row: Any) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "chat_type": row["chat_type"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "sender_role": row["sender_role"],
        "sent_at": row["sent_at"],
        "text": row["text"],
        "thread_id": row["thread_id"],
        "reply_to_message_id": row["reply_to_message_id"],
    }


def _task_card(task: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": task.short_id,
        "status": task.status,
        "chat_id": task.chat_id,
        "chat_type": task.chat_type,
        "root_message_id": task.root_message_id,
        "task_label": task.task_label,
        "watch_until": task.watch_until,
    }


def _candidate_card(task: TaskRecord, matched_by: str) -> dict[str, Any]:
    return _task_card(task) | {"matched_by": matched_by}


def _resource_card(row: Any) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "file_key": row["file_key"],
        "resource_type": row["resource_type"],
        "download_status": row["download_status"],
        "path": row["path"],
    }
