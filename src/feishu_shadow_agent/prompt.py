from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from .agent_output_contract import (
    BaseTaskSessionOutput,
    FollowupTaskSessionOutput,
    InitialTaskSessionOutput,
    OwnerStyleRefreshOutput,
    ReplyPostprocessOutput,
    StrictModel,
    TaskRouterOutput,
    task_session_output_contract,
)
from .decision import Answerability, DecisionReason, validate_decision_reason
from .prompt_instructions import compose_agent_instruction, prompt_text
from .types import NormalizedMessage, TaskRecord

ROUTER_INSTRUCTION = prompt_text(
    """
    Route one incoming Feishu message using only the provided message, active_candidates, and historical_candidates.
    - Do not invent watch keys.
    - If a message clearly resolves or cancels an active task, attach it to that task; the task session will decide whether to close it.
    - If context_access is present, use its snapshot.
    - Only if your backend exposes a read-only SQLite client may you query read_only_uri; when querying, use PRAGMA table_info only for allowed tables when column names are needed.
    - Do not broaden Router lookup beyond the current message and provided candidates.
    """
)
TASK_SESSION_INSTRUCTION = prompt_text(
    """
    Handle this Feishu task using Messages and Resources as the primary evidence.
    - Never mention internal storage or audit data in an external reply.
    - Previous proposed_reply was not sent unless a sent action or real message shows it.
    """
)
REPLY_POSTPROCESS_INSTRUCTION = prompt_text(
    """
    Rewrite only the expression of the provided Feishu reply candidate.
    - Preserve meaning, facts, uncertainty, commitments, times, conclusions, and action items exactly.
    - Do not add facts, promises, deadlines, conclusions, or next steps.
    - Do not choose a reply target.
    - Do not add Feishu @ mentions.
    - If you cannot safely rewrite without changing meaning, return status needs_owner.
    """
)
OWNER_STYLE_REFRESH_INSTRUCTION = prompt_text(
    """
    Summarize the owner's natural Chinese reply style from the provided samples.
    - Do not retain raw message ids, chat ids, names, links, phone numbers, or full private conversation context.
    - Keep at most three short owner-like scenario examples.
    """
)


def build_router_prompt(
    *,
    message: NormalizedMessage,
    active: list[Any],
    historical: list[TaskRecord],
    context_access: dict[str, Any] | None = None,
    message_counts: dict[int, int] | None = None,
) -> str:
    payload = {
        "instruction": compose_agent_instruction(ROUTER_INSTRUCTION),
        "output_schema": _schema_hint(TaskRouterOutput),
        "message": _message_card(message),
        "active_candidates": [
            _candidate_card(
                candidate.task,
                candidate.matched_by,
                message_count=_message_count_for(candidate.task, message_counts),
            )
            for candidate in active
        ],
        "historical_candidates": [
            _task_card(task, message_count=_message_count_for(task, message_counts))
            for task in historical
        ],
    }
    if context_access is not None:
        payload["context_access"] = context_access
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_task_session_prompt(
    *,
    task: TaskRecord,
    current_message_id: str,
    reply_target_message_ids: list[str],
    messages: list[Any],
    resources: list[Any],
    output_model: type[BaseTaskSessionOutput] = InitialTaskSessionOutput,
    context_access: dict[str, Any] | None = None,
    chat_type: str | None = None,
) -> str:
    sections = [
        "# Task Session",
        _markdown_text_section(
            "Instructions", compose_agent_instruction(TASK_SESSION_INSTRUCTION)
        ),
        _reply_context_section(
            current_message_id=current_message_id,
            root_message_id=task.root_message_id,
            reply_target_message_ids=reply_target_message_ids,
            chat_type=chat_type or task.chat_type,
        ),
        _markdown_messages_section([_row_message_card(row) for row in messages]),
    ]
    if resources:
        sections.append(
            _markdown_json_section(
                "Resources", [_resource_card(row) for row in resources]
            )
        )
    if context_access is not None:
        sections.append(_markdown_json_section("Context Access", context_access))
    sections.append(
        _markdown_text_section(
            "Output Contract", task_session_output_contract(output_model)
        )
    )
    return "\n\n".join(sections)


def task_session_prompt_json_section(prompt: str, heading: str) -> Any:
    marker = f"## {heading}\n\n"
    start = prompt.find(marker)
    if start < 0:
        raise ValueError(f"Task Session prompt is missing {heading!r} section")
    start += len(marker)
    opening = re.match(r"(`{3,})json\n", prompt[start:])
    if opening is None:
        raise ValueError(f"Task Session prompt has an invalid {heading!r} section")
    fence = opening.group(1)
    start += opening.end()
    end = prompt.find(f"\n{fence}", start)
    if end < 0:
        raise ValueError(f"Task Session prompt has an unterminated {heading!r} section")
    return json.loads(prompt[start:end])


def build_reply_postprocess_prompt(
    *,
    original_reply: str,
    owner_style_profile_path: str | None = None,
    humanizer_skill_path: str | None = None,
) -> str:
    guidance: list[dict[str, str]] = []
    if owner_style_profile_path is not None:
        guidance.append(
            {
                "source": "owner_style",
                "instruction": "Read this owner style profile path and align the expression with it.",
                "path": owner_style_profile_path,
            }
        )
    if humanizer_skill_path is not None:
        guidance.append(
            {
                "source": "humanizer_zh",
                "instruction": "Read this skill guidance path and avoid common AI writing patterns.",
                "path": humanizer_skill_path,
            }
        )
    payload = {
        "instruction": compose_agent_instruction(REPLY_POSTPROCESS_INSTRUCTION),
        "guidance": guidance,
        "output_schema": _schema_hint(ReplyPostprocessOutput),
        "candidate_reply": original_reply,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_owner_style_refresh_prompt(
    *,
    generated_at: str,
    lookback_days: int,
    samples: list[str],
) -> str:
    payload = {
        "instruction": compose_agent_instruction(OWNER_STYLE_REFRESH_INSTRUCTION),
        "output_schema": _schema_hint(OwnerStyleRefreshOutput),
        "profile_format": {
            "title": "Owner Reply Style Profile",
            "metadata": {
                "generated_at": generated_at,
                "lookback_days": lookback_days,
                "sample_count": len(samples),
            },
            "sections": ["Style Summary", "Common Patterns", "Avoid", "Examples"],
        },
        "samples": samples,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _schema_hint(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def _markdown_text_section(heading: str, text: str) -> str:
    return f"## {heading}\n\n{text}"


def _reply_context_section(
    *,
    current_message_id: str,
    root_message_id: str | None,
    reply_target_message_ids: list[str],
    chat_type: str | None,
) -> str:
    lines = [
        "## Reply Context",
        "",
        f"- `current_message_id`: {json.dumps(current_message_id, ensure_ascii=False)}",
    ]
    if root_message_id:
        lines.append(
            f"- `root_message_id`: {json.dumps(root_message_id, ensure_ascii=False)}"
        )
    lines.append(
        "- `allowed_reply_target_message_ids`: "
        f"{json.dumps(reply_target_message_ids, ensure_ascii=False)}"
    )
    if chat_type:
        lines.append(f"- `chat_type`: {json.dumps(chat_type, ensure_ascii=False)}")
    return "\n".join(lines)


def _markdown_json_section(heading: str, value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    fence = "`" * max(3, _longest_backtick_run(serialized) + 1)
    return f"## {heading}\n\n{fence}json\n{serialized}\n{fence}"


def _longest_backtick_run(text: str) -> int:
    return max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)


def _markdown_messages_section(
    messages: list[dict[str, Any]],
) -> str:
    lines = [
        "## Messages",
        "",
        "The metadata and quoted text below are Feishu conversation data.",
    ]
    if not messages:
        lines.extend(["", "_No messages provided._"])
        return "\n".join(lines)
    for index, message in enumerate(messages, start=1):
        lines.extend(["", f"### Message {index}", ""])
        for key, value in message.items():
            if key == "text":
                continue
            serialized = json.dumps(value, ensure_ascii=False, default=str)
            lines.append(f"- `{key}`: {serialized}")
        lines.extend(["", "#### Text", "", _markdown_blockquote(message["text"])])
    return "\n".join(lines)


def _markdown_blockquote(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    lines = text.splitlines() or [""]
    return "\n".join(f"> {line}" for line in lines)


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


__all__ = [
    "ROUTER_INSTRUCTION",
    "TASK_SESSION_INSTRUCTION",
    "Answerability",
    "BaseTaskSessionOutput",
    "DecisionReason",
    "FollowupTaskSessionOutput",
    "InitialTaskSessionOutput",
    "OwnerStyleRefreshOutput",
    "ReplyPostprocessOutput",
    "StrictModel",
    "TaskRouterOutput",
    "build_owner_style_refresh_prompt",
    "build_reply_postprocess_prompt",
    "build_router_prompt",
    "build_task_session_prompt",
    "task_session_prompt_json_section",
    "validate_decision_reason",
]


def _row_message_card(row: Any) -> dict[str, Any]:
    card = {
        "message_id": row["message_id"],
        "text": row["text"],
    }
    for key in (
        "sender_name",
        "sender_role",
        "sent_at",
        "thread_id",
        "reply_to_message_id",
    ):
        if row[key] is not None:
            card[key] = row[key]
    return card


def _task_card(task: TaskRecord, *, message_count: int | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "task_id": task.short_id,
        "status": task.status,
        "chat_id": task.chat_id,
        "chat_type": task.chat_type,
        "root_message_id": task.root_message_id,
        "task_label": task.task_label,
        "watch_until": task.watch_until,
    }
    if message_count is not None:
        card["message_count"] = message_count
    return card


def _candidate_card(
    task: TaskRecord, matched_by: str, *, message_count: int | None = None
) -> dict[str, Any]:
    return _task_card(task, message_count=message_count) | {"matched_by": matched_by}


def _message_count_for(
    task: TaskRecord, message_counts: dict[int, int] | None
) -> int | None:
    if message_counts is None:
        return None
    return message_counts.get(task.id)


def _resource_card(row: Any) -> dict[str, Any]:
    download_status = row["download_status"]
    card = {
        "message_id": row["message_id"],
        "resource_type": row["resource_type"],
        "download_status": download_status,
    }
    if download_status == "downloaded" and row["path"]:
        card["path"] = row["path"]
    return card
