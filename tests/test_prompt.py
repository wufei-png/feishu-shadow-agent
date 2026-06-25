from __future__ import annotations

import json

from feishu_shadow_agent.prompt import (
    TaskRouterOutput,
    TaskSessionOutput,
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

    prompt = json.loads(
        build_router_prompt(message=message, active=[TaskCandidate(task=task, matched_by="thread")], historical=[])
    )

    assert prompt["output_schema"] == TaskRouterOutput.model_json_schema()
    assert prompt["output_schema"]["additionalProperties"] is False
    assert prompt["output_schema"]["properties"]["route"]["enum"] == [
        "new_task",
        "attach_task",
        "reopen_task",
        "close_task",
        "ignore",
        "ambiguous",
    ]
    assert "schema" not in prompt
    assert prompt["active_candidates"][0]["matched_by"] == "thread"


def test_task_session_prompt_embeds_pydantic_output_schema() -> None:
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

    prompt = json.loads(
        build_task_session_prompt(
            task=task,
            current_message_id="om_1",
            reply_target_message_ids=["om_1", "om_root"],
            messages=rows,
            resources=resources,
        )
    )

    assert prompt["output_schema"] == TaskSessionOutput.model_json_schema()
    assert prompt["output_schema"]["additionalProperties"] is False
    assert "confidence" not in prompt["output_schema"]["properties"]
    assert "watch_extend_minutes" not in prompt["output_schema"]["properties"]
    assert "requires_resources" not in prompt["output_schema"]["properties"]
    assert "schema" not in prompt
    assert prompt["metadata"]["reply_target_message_ids"] == ["om_1", "om_root"]
    assert prompt["resources"][0]["path"] == "data/resources/om_1/img_1.jpg"
