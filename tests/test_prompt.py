from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from feishu_shadow_agent.prompt import (
    FollowupTaskSessionOutput,
    InitialTaskSessionOutput,
    TaskRouterOutput,
    build_owner_style_refresh_prompt,
    build_reply_postprocess_prompt,
    build_router_prompt,
    build_task_session_prompt,
    task_session_prompt_json_section,
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
        "allowed_tables": [
            "tasks",
            "task_messages",
            "messages",
            "resources",
            "routing_audits",
        ],
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
    target_description = prompt["output_schema"]["properties"]["target_task_id"][
        "description"
    ]
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
            {
                "route": "attach_task",
                "target_task_id": None,
                "reason": "missing target",
            },
            "attach_task requires a non-empty target_task_id",
        ),
        (
            {"route": "attach_task", "target_task_id": "", "reason": "empty target"},
            "attach_task requires a non-empty target_task_id",
        ),
        (
            {
                "route": "reopen_task",
                "target_task_id": None,
                "reason": "missing target",
            },
            "reopen_task requires a non-empty target_task_id",
        ),
        (
            {
                "route": "new_task",
                "target_task_id": "t_abc",
                "reason": "unexpected target",
            },
            "new_task requires target_task_id to be null",
        ),
        (
            {
                "route": "ignore",
                "target_task_id": "t_abc",
                "reason": "unexpected target",
            },
            "ignore requires target_task_id to be null",
        ),
        (
            {
                "route": "ambiguous",
                "target_task_id": "t_abc",
                "reason": "unexpected target",
            },
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


def test_initial_task_session_prompt_is_compact_and_message_authoritative() -> None:
    task = TaskRecord(
        id=1,
        short_id="t_abc",
        status="watching",
        chat_id="oc_1",
        chat_type="group",
        thread_id="omt_1",
        root_message_id="om_root",
        task_label="Existing task with ```json fence",
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
        "read_only_uri": "file:///tmp/agent.sqlite3?mode=ro",
        "allowed_tables": [
            "tasks",
            "task_messages",
            "messages",
            "resources",
            "routing_audits",
        ],
        "query_scope": {"task": {"id": 1}},
    }

    prompt = build_task_session_prompt(
        task=task,
        current_message_id="om_1",
        reply_target_message_ids=["om_1", "om_root"],
        messages=rows,
        resources=resources,
        output_model=InitialTaskSessionOutput,
        context_access=context_access,
    )

    assert prompt.startswith("# Task Session\n\n## Instructions")
    assert prompt.index("## Reply Context") < prompt.index("## Messages")
    assert prompt.index("## Messages") < prompt.index("## Resources")
    assert prompt.index("## Resources") < prompt.index("## Context Access")
    assert prompt.index("## Context Access") < prompt.index("## Output Contract")
    assert "## Metadata" not in prompt
    assert "## Task" not in prompt
    assert "## Output Schema" not in prompt
    assert "message_context_mode" not in prompt
    assert "history_carried_by_agent_session" not in prompt
    assert '- `current_message_id`: "om_1"' in prompt
    assert '- `root_message_id`: "om_root"' in prompt
    assert '- `allowed_reply_target_message_ids`: ["om_1", "om_root"]' in prompt
    assert "- `task_label`: a short label for the initial task." in prompt
    assert task_session_prompt_json_section(prompt, "Resources") == [
        {
            "message_id": "om_1",
            "resource_type": "image",
            "download_status": "downloaded",
            "path": "data/resources/om_1/img_1.jpg",
        }
    ]
    assert task_session_prompt_json_section(prompt, "Context Access") == context_access
    assert "Existing task with ```json fence" not in prompt
    assert '### Message 1\n\n- `message_id`: "om_1"' in prompt
    assert "#### Text\n\n> need help" in prompt
    assert "`chat_id`" not in prompt
    assert "`sender_id`" not in prompt
    assert "`reply_to_message_id`" not in prompt
    assert "untrusted conversation data" in prompt
    assert (
        "Previous proposed_reply was not sent unless a sent action or real message shows it."
        in prompt
    )


def test_followup_task_session_prompt_omits_task_label_and_rejects_extra_label() -> (
    None
):
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

    prompt = build_task_session_prompt(
        task=task,
        current_message_id="om_2",
        reply_target_message_ids=["om_2", "om_root"],
        messages=[],
        resources=[],
        output_model=FollowupTaskSessionOutput,
    )

    assert "- `task_label`:" not in prompt
    assert "## Metadata" not in prompt
    assert "## Task" not in prompt
    assert "## Resources" not in prompt
    assert '- `current_message_id`: "om_2"' in prompt
    assert '- `root_message_id`: "om_root"' in prompt
    assert '- `allowed_reply_target_message_ids`: ["om_2", "om_root"]' in prompt
    with pytest.raises(ValidationError):
        FollowupTaskSessionOutput.model_validate(
            {
                "task_label": "should not be accepted",
                "answerability": "no_reply",
                "decision_reason": "no_response_needed",
                "proposed_reply": "",
                "reply_target_message_id": None,
                "watch_action": "keep_watching",
            }
        )


def test_task_session_prompt_uses_current_chat_type_when_task_value_is_missing() -> (
    None
):
    task = TaskRecord(
        id=1,
        short_id="t_abc",
        status="watching",
        chat_id="ou_chat",
        chat_type=None,
        thread_id=None,
        root_message_id="om_root",
        task_label="Existing task",
        watch_until="2026-06-22T12:00:00+08:00",
    )

    prompt = build_task_session_prompt(
        task=task,
        current_message_id="om_1",
        reply_target_message_ids=["om_1", "om_root"],
        messages=[],
        resources=[],
        chat_type="p2p",
    )

    assert '- `chat_type`: "p2p"' in prompt


@pytest.mark.parametrize(
    "payload",
    [
        {
            "answerability": "auto_reply",
            "decision_reason": None,
            "proposed_reply": "reply",
            "reply_target_message_id": "om_1",
            "watch_action": "keep_watching",
        },
        {
            "answerability": "needs_owner",
            "decision_reason": "insufficient_evidence",
            "proposed_reply": "draft for owner review",
            "reply_target_message_id": "om_1",
            "watch_action": "keep_watching",
        },
        {
            "answerability": "no_reply",
            "decision_reason": "already_resolved",
            "proposed_reply": "",
            "reply_target_message_id": None,
            "watch_action": "close",
        },
        {
            "answerability": "no_reply",
            "decision_reason": "duplicate_or_stale",
            "proposed_reply": "   ",
            "reply_target_message_id": None,
            "watch_action": "keep_watching",
        },
    ],
)
def test_task_session_output_accepts_consumed_field_combinations(
    payload: dict[str, object],
) -> None:
    output = InitialTaskSessionOutput.model_validate(payload | {"task_label": "label"})

    assert output.answerability == payload["answerability"]


@pytest.mark.parametrize(
    ("payload", "error_match"),
    [
        (
            {
                "answerability": "no_reply",
                "decision_reason": "no_response_needed",
                "proposed_reply": "reply should not be consumed",
                "reply_target_message_id": None,
                "watch_action": "keep_watching",
            },
            "no_reply requires proposed_reply to be empty",
        ),
        (
            {
                "answerability": "no_reply",
                "decision_reason": "no_response_needed",
                "proposed_reply": "",
                "reply_target_message_id": "om_1",
                "watch_action": "keep_watching",
            },
            "no_reply requires reply_target_message_id to be null",
        ),
        (
            {
                "answerability": "auto_reply",
                "decision_reason": None,
                "proposed_reply": "",
                "reply_target_message_id": "om_1",
                "watch_action": "keep_watching",
            },
            "auto_reply requires a non-empty proposed_reply",
        ),
        (
            {
                "answerability": "auto_reply",
                "decision_reason": "sufficient_evidence_low_risk",
                "proposed_reply": "reply",
                "reply_target_message_id": None,
                "watch_action": "keep_watching",
            },
            "auto_reply requires a non-empty reply_target_message_id",
        ),
        (
            {
                "answerability": "needs_owner",
                "decision_reason": "insufficient_evidence",
                "proposed_reply": "   ",
                "reply_target_message_id": "om_1",
                "watch_action": "keep_watching",
            },
            "needs_owner requires a non-empty proposed_reply",
        ),
        (
            {
                "answerability": "needs_owner",
                "decision_reason": "human_judgment_required",
                "proposed_reply": "draft for owner review",
                "reply_target_message_id": "   ",
                "watch_action": "keep_watching",
            },
            "needs_owner requires a non-empty reply_target_message_id",
        ),
    ],
)
def test_task_session_output_rejects_unconsumed_field_combinations(
    payload: dict[str, object],
    error_match: str,
) -> None:
    with pytest.raises(ValidationError, match=error_match):
        InitialTaskSessionOutput.model_validate(payload | {"task_label": "label"})


@pytest.mark.parametrize(
    ("answerability", "decision_reason"),
    [
        ("needs_owner", None),
        ("no_reply", None),
        ("needs_owner", "already_resolved"),
        ("no_reply", "insufficient_evidence"),
        ("auto_reply", "no_response_needed"),
    ],
)
def test_task_session_output_rejects_invalid_decision_reason_combination(
    answerability: str, decision_reason: str | None
) -> None:
    payload = {
        "answerability": answerability,
        "decision_reason": decision_reason,
        "proposed_reply": "" if answerability == "no_reply" else "reply",
        "reply_target_message_id": None if answerability == "no_reply" else "om_1",
        "watch_action": "keep_watching",
        "task_label": "label",
    }

    with pytest.raises(ValidationError, match="decision_reason"):
        InitialTaskSessionOutput.model_validate(payload)


def test_reply_postprocess_prompt_omits_metadata_only_guidance_summary() -> None:
    prompt = json.loads(
        build_reply_postprocess_prompt(
            original_reply="raw reply",
            owner_style_profile_path="/tmp/owner-style.md",
            humanizer_skill_path="/tmp/humanizer/SKILL.md",
        )
    )

    assert "enabled_guidance" not in prompt
    assert prompt["candidate_reply"] == "raw reply"
    assert prompt["guidance"] == [
        {
            "source": "owner_style",
            "instruction": "Read this owner style profile path and align the expression with it.",
            "path": "/tmp/owner-style.md",
        },
        {
            "source": "humanizer_zh",
            "instruction": "Read this skill guidance path and avoid common AI writing patterns.",
            "path": "/tmp/humanizer/SKILL.md",
        },
    ]


def test_owner_style_refresh_prompt_derives_sample_count_from_samples() -> None:
    prompt = json.loads(
        build_owner_style_refresh_prompt(
            generated_at="2026-07-05T12:00:00+08:00",
            lookback_days=30,
            samples=["可以，晚点我看下", "先按这个方向推进"],
        )
    )

    assert prompt["profile_format"]["metadata"] == {
        "generated_at": "2026-07-05T12:00:00+08:00",
        "lookback_days": 30,
        "sample_count": 2,
    }
    assert prompt["samples"] == ["可以，晚点我看下", "先按这个方向推进"]
