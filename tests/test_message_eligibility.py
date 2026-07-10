from __future__ import annotations

import pytest

from feishu_shadow_agent.ingestion import MessageNormalizer
from feishu_shadow_agent.message_eligibility import MessageEligibilityPolicy


@pytest.mark.parametrize(
    ("raw", "sources", "decision", "reason"),
    [
        ({}, [], "dropped", "not_acquired"),
        ({"sender_type": "bot"}, ["group_at_me"], "dropped", "self_message"),
        ({"at_all": True}, ["group_at_me"], "dropped", "at_all_suppressed"),
        (
            {"sender_id": "ou_owner"},
            ["active_watch"],
            "kept",
            "owner_intervention",
        ),
        (
            {"mentions": [{"open_id": "ou_owner"}]},
            ["group_at_me"],
            "kept",
            "direct_owner_mention",
        ),
        ({}, ["active_watch"], "kept", "active_watch_message"),
        ({}, ["group_at_me"], "dropped", "non_direct_mention"),
    ],
)
def test_group_message_eligibility_reason_precedence(
    raw, sources, decision, reason
) -> None:
    message = MessageNormalizer(owner_open_id="ou_owner").normalize(
        {
            "message_id": "om_1",
            "chat_id": "oc_1",
            "chat_type": "group",
            "sender_id": "ou_user",
            "create_time": "2026-07-10T10:00:00+08:00",
            **raw,
        }
    )

    result = MessageEligibilityPolicy().decide(message, sources=sources)

    assert result.decision == decision
    assert result.reason_code == reason


def test_p2p_active_watch_is_eligible() -> None:
    message = MessageNormalizer(owner_open_id="ou_owner").normalize(
        {
            "message_id": "om_1",
            "chat_id": "oc_1",
            "chat_type": "p2p",
            "sender_id": "ou_user",
            "create_time": "2026-07-10T10:00:00+08:00",
        }
    )

    result = MessageEligibilityPolicy().decide(message, sources=["active_watch"])

    assert result.decision == "kept"
    assert result.reason_code == "active_watch_message"
