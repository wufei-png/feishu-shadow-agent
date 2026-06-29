from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from feishu_shadow_agent.prompt import (
    FollowupTaskSessionOutput,
    InitialTaskSessionOutput,
    TaskRouterOutput,
    build_router_prompt,
    build_task_session_prompt,
)
from feishu_shadow_agent.types import NormalizedMessage, TaskCandidate, TaskRecord


def test_router_prompt_embeds_pydantic_output_schema() -> None:
    message = NormalizedMessage(
        message_id="om_1",
        chat_id="oc_1",
        chat_type="group",
        sender_id="ou_ext",
        sender_name="Ext",
        sender_type="user",
        sender_role="external_user_message",
        sent_at="2026-06-22T10:00:00+08:00",
        thread_id="omt_1",
        reply_to_message_id=None,
        text="need help",
        direct_mention=True,
        at_all=False,
    )
    task = TaskRecord(
        id=1,
        short_id="t_abc",
        status="watching",
        chat_id="oc_1",
        chat_type="group",
        thread_id="omt_1",
        root_message_id="om_root",
        task_label="Existing task",
        watch_until="2026-06-22T12:00:00+08:00",
    )

    context_access = {
        "backend": "sqlite",
        "mode": "live_read_only",
        "read_only_uri": "file:///tmp/agent.sqlite3?mode=ro",
        "allowed_tables": ["tasks", "task_messages", "messages", "resources", "routing_audits"],
        "query_scope": {
            "current_message_id": "om_1",
            "active_tasks": [{"id": 1, "short_id": "t_abc"}],
            "historical_tasks": [],
        },
    }

    prompt = json.loads(
        build_router_prompt(
            message=message,
            active=[TaskCandidate(task=task, matched_by="thread")],
            historical=[],
            context_access=context_access,
            message_counts={task.id: 3},
        )
    )

    assert prompt["output_schema"] == TaskRouterOutput.model_json_schema()
    assert prompt["output_schema"]["additionalProperties"] is False
    assert "Do not invent task ids" in prompt["instruction"]
    assert "context_access" in prompt["instruction"]
    assert prompt["output_schema"]["properties"]["route"]["enum"] == [
        "new_task",
        "attach_task",
        "reopen_task",
        "ignore",
        "ambiguous",
    ]
    route_description = prompt["output_schema"]["properties"]["route"]["description"]
    target_description = prompt["output_schema"]["properties"]["target_task_id"]["description"]
    assert "attach_task appends to one active candidate" in route_description
    assert "Must be null for new_task" in target_description
    assert "schema" not in prompt
    assert prompt["active_candidates"][0]["matched_by"] == "thread"
    assert prompt["active_candidates"][0]["message_count"] == 3
    assert "context_access" not in prompt["active_candidates"][0]
    assert prompt["context_access"] == context_access


@pytest.mark.parametrize(
    "payload,error_match",
    [
        (
            {"route": "attach_task", "target_task_id": None, "reason": "missing target"},
            "attach_task requires a non-empty target_task_id",
        ),
        (
            {"route": "attach_task", "target_task_id": "", "reason": "empty target"},
            "attach_task requires a non-empty target_task_id",
        ),
        (
            {"route": "reopen_task", "target_task_id": None, "reason": "missing target"},
            "reopen_task requires a non-empty target_task_id",
        ),
        (
            {"route": "new_task", "target_task_id": "t_abc", "reason": "unexpected target"},
            "new_task requires target_task_id to be null",
        ),
        (
            {"route": "ignore", "target_task_id": "t_abc", "reason": "unexpected target"},
            "ignore requires target_task_id to be null",
        ),
        (
            {"route": "ambiguous", "target_task_id": "t_abc", "reason": "unexpected target"},
            "ambiguous requires target_task_id to be null",
        ),
    ],
)
def test_task_router_output_rejects_malformed_route_target_pairs(
    payload: dict[str, object],
    error_match: str,
) -> None:
    with pytest.raises(ValidationError, match=error_match):
        TaskRouterOutput.model_validate(payload)


def test_initial_task_session_prompt_embeds_pydantic_output_schema() -> None:
    task = TaskRecord(
        id=1,
        short_id="t_abc",
        status="watching",
        chat_id="oc_1",
        chat_type="group",
        thread_id="omt_1",
        root_message_id="om_root",
        task_label="Existing task",
        watch_until="2026-06-22T12:00:00+08:00",
    )
    rows = [
        {
            "message_id": "om_1",
            "chat_id": "oc_1",
            "chat_type": "group",
            "sender_id": "ou_ext",
            "sender_name": "Ext",
            "sender_role": "external_user_message",
            "sent_at": "2026-06-22T10:00:00+08:00",
            "text": "need help",
            "thread_id": "omt_1",
            "reply_to_message_id": None,
        }
    ]
    resources = [
        {
            "message_id": "om_1",
            "file_key": "img_1",
            "resource_type": "image",
            "download_status": "downloaded",
            "path": "data/resources/om_1/img_1.jpg",
        }
    ]

    context_access = {
        "backend": "sqlite",
        "mode": "live_read_only",
        "read_only_uri": "file:///tmp/agent.sqlite3?mode=ro",
        "allowed_tables": ["tasks", "task_messages", "messages", "resources", "routing_audits"],
        "query_scope": {
            "current_message_id": "om_1",
            "task": {"id": 1, "short_id": "t_abc"},
        },
    }

    prompt = json.loads(
        build_task_session_prompt(
            task=task,
            current_message_id="om_1",
            reply_target_message_ids=["om_1", "om_root"],
            messages=rows,
            resources=resources,
            output_model=InitialTaskSessionOutput,
            context_metadata={
                "message_context_mode": "full_task_messages",
                "included_message_count": 1,
                "task_message_count": 1,
                "history_carried_by_agent_session": False,
            },
            context_access=context_access,
        )
    )

    assert prompt["output_schema"] == InitialTaskSessionOutput.model_json_schema()
    assert prompt["output_schema"]["additionalProperties"] is False
    assert "task_label" in prompt["output_schema"]["properties"]
    assert "confidence" not in prompt["output_schema"]["properties"]
    assert "watch_extend_minutes" not in prompt["output_schema"]["properties"]
    assert "requires_resources" not in prompt["output_schema"]["properties"]
    assert "context_access" in prompt["instruction"]
    assert "Only messages in the messages block are real Feishu messages" in prompt["instruction"]
    assert "Previous proposed_reply outputs are not sent" in prompt["instruction"]
    assert "schema" not in prompt
    assert prompt["metadata"]["reply_target_message_ids"] == ["om_1", "om_root"]
    assert prompt["metadata"]["message_context_mode"] == "full_task_messages"
    assert prompt["resources"][0]["path"] == "data/resources/om_1/img_1.jpg"
    assert prompt["context_access"] == context_access


def test_followup_task_session_prompt_omits_task_label_and_rejects_extra_label() -> None:
    task = TaskRecord(
        id=1,
        short_id="t_abc",
        status="watching",
        chat_id="oc_1",
        chat_type="group",
        thread_id="omt_1",
        root_message_id="om_root",
        task_label="Existing task",
        watch_until="2026-06-22T12:00:00+08:00",
    )

    prompt = json.loads(
        build_task_session_prompt(
            task=task,
            current_message_id="om_2",
            reply_target_message_ids=["om_2", "om_root"],
            messages=[],
            resources=[],
            output_model=FollowupTaskSessionOutput,
            context_metadata={
                "message_context_mode": "incremental_current_message",
                "included_message_count": 1,
                "task_message_count": 2,
                "history_carried_by_agent_session": True,
            },
        )
    )

    assert prompt["output_schema"] == FollowupTaskSessionOutput.model_json_schema()
    assert "task_label" not in prompt["output_schema"]["properties"]
    assert prompt["metadata"]["message_context_mode"] == "incremental_current_message"
    assert prompt["metadata"]["history_carried_by_agent_session"] is True
    with pytest.raises(ValidationError):
        FollowupTaskSessionOutput.model_validate(
            {
                "task_label": "should not be accepted",
                "answerability": "no_reply",
                "proposed_reply": "",
                "reply_target_message_id": None,
                "watch_action": "keep_watching",
            }
        )
