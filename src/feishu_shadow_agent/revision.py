from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, cast

from .decision import DecisionReason
from .types import NormalizedMessage

RevisionImpact = Literal["none", "low", "high", "uncertain"]


def message_semantic_hash(message: NormalizedMessage) -> str:
    payload = {
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
        "resources": sorted(
            (resource.file_key, resource.resource_type)
            for resource in message.resources
        ),
        "is_deleted": message.is_deleted,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(serialized.encode("utf-8")).hexdigest()


HIGH_IMPACT_DECISION_REASONS = frozenset(
    {
        "commitment_or_authorization",
        "sensitive_or_high_impact",
        "write_or_permission",
    }
)
HIGH_IMPACT_SIGNALS = frozenset(
    {
        "factual_correction",
        "commitment_change",
        "permission_change",
        "sensitive_change",
        "uncertain",
    }
)


@dataclass(frozen=True)
class RevisionAssessment:
    impact: RevisionImpact
    reasons: tuple[str, ...]
    reply_changed: bool


@dataclass(frozen=True)
class RevisionReviewPlan:
    action: Literal["audit", "notify_identical", "notify_low", "approval"]
    assessment: RevisionAssessment
    payload: dict[str, Any]


def plan_no_reply_review(
    *,
    message: NormalizedMessage,
    previous_reply: dict[str, Any],
    decision_reason: DecisionReason | None,
) -> dict[str, Any]:
    return {
        "type": "revision_correction_review",
        "message_id": message.message_id,
        "source_message_id": message.message_id,
        "source_revision": message.revision,
        "previous_reply": previous_reply.get("text", ""),
        "previous_action_id": previous_reply.get("action_id"),
        "suggested_reply": "",
        "impact": "uncertain",
        "impact_reasons": ["new_revision_no_reply"],
        "decision_reason": decision_reason,
        "resolution_options": ["send_correction", "no_action"],
        "edit_supported": False,
        "correction_delivery": "explicit_message",
        "dedupe_key": f"revision-review:{message.message_id}:{message.revision}",
    }


def plan_reply_review(
    *,
    message: NormalizedMessage,
    task_short_id: str,
    previous_reply: dict[str, Any],
    current_reply: str,
    answerability: str,
    decision_reason: DecisionReason | None,
    target_message_id: str,
    destination: dict[str, str],
    revision_signals: Iterable[str] = (),
) -> RevisionReviewPlan:
    previous_payload = previous_reply.get("payload")
    previous_destination = (
        cast(dict[str, Any], previous_payload).get("reply_destination")
        if isinstance(previous_payload, dict)
        else None
    )
    previous_destination = (
        cast(dict[str, str], previous_destination)
        if isinstance(previous_destination, dict)
        else None
    )
    assessment = assess_revision_impact(
        previous_reply=str(previous_reply.get("text") or ""),
        current_reply=current_reply,
        answerability=answerability,
        decision_reason=decision_reason,
        current_target_message_id=target_message_id,
        previous_target_message_id=previous_reply.get("target_message_id"),
        current_reply_destination=destination,
        previous_reply_destination=previous_destination,
        revision_signals=revision_signals,
    )
    payload: dict[str, Any] = {
        "type": "revision_correction_review",
        "message_id": message.message_id,
        "source_message_id": message.message_id,
        "source_revision": message.revision,
        "previous_reply": previous_reply.get("text", ""),
        "previous_action_id": previous_reply.get("action_id"),
        "previous_reply_destination": previous_destination,
        "reply_destination": destination,
        "suggested_reply": current_reply,
        "impact": assessment.impact,
        "impact_reasons": list(assessment.reasons),
        "decision_reason": decision_reason,
        "resolution_options": ["send_correction", "no_action"],
        "edit_supported": False,
        "correction_delivery": "explicit_message",
        "dedupe_key": f"revision-review:{message.message_id}:{message.revision}",
    }
    if "reply_destination_changed" in assessment.reasons:
        payload["commands"] = [
            f"/send {task_short_id} --location {destination['location']} <final reply>"
        ]
    if assessment.impact == "none":
        action = "audit"
    elif not assessment.reply_changed:
        action = "notify_identical"
    elif assessment.impact == "low":
        action = "notify_low"
    else:
        action = "approval"
        payload["requires_owner_approval"] = True
    return RevisionReviewPlan(action=action, assessment=assessment, payload=payload)


def assess_revision_impact(
    *,
    previous_reply: str,
    current_reply: str,
    answerability: str,
    decision_reason: DecisionReason | None,
    current_target_message_id: str | None,
    previous_target_message_id: str | None,
    current_reply_destination: Mapping[str, str] | None = None,
    previous_reply_destination: Mapping[str, str] | None = None,
    revision_signals: Iterable[str] = (),
) -> RevisionAssessment:
    """Classify a correction review; agent signals can only escalate risk."""

    reply_changed = _canonical_reply(previous_reply) != _canonical_reply(current_reply)
    target_changed = current_target_message_id != previous_target_message_id
    destination_changed = (
        current_reply_destination is not None
        and previous_reply_destination is not None
        and current_reply_destination != previous_reply_destination
    )
    signals = {str(signal) for signal in revision_signals}
    reasons: list[str] = []
    if reply_changed:
        reasons.append("reply_changed")
    if target_changed:
        reasons.append("reply_target_changed")
    if destination_changed:
        reasons.append("reply_destination_changed")
    if answerability == "needs_owner":
        reasons.append("needs_owner")
    if decision_reason in HIGH_IMPACT_DECISION_REASONS:
        reasons.append(str(decision_reason))
    high_signals = signals & HIGH_IMPACT_SIGNALS
    reasons.extend(sorted(high_signals))
    decision_changed = answerability != "auto_reply" or decision_reason not in {
        None,
        "sufficient_evidence_low_risk",
    }

    if (
        not reply_changed
        and not target_changed
        and not destination_changed
        and not decision_changed
        and not high_signals
    ):
        return RevisionAssessment("none", tuple(reasons), reply_changed=False)
    if (
        high_signals
        or answerability == "needs_owner"
        or decision_reason in HIGH_IMPACT_DECISION_REASONS
        or target_changed
        or destination_changed
    ):
        return RevisionAssessment("high", tuple(dict.fromkeys(reasons)), reply_changed)
    if answerability == "auto_reply" and decision_reason in {
        None,
        "sufficient_evidence_low_risk",
    }:
        return RevisionAssessment("low", tuple(dict.fromkeys(reasons)), reply_changed)
    return RevisionAssessment(
        "uncertain",
        tuple(dict.fromkeys([*reasons, "insufficient_revision_evidence"])),
        reply_changed,
    )


def _canonical_reply(value: str) -> str:
    return " ".join(value.split())


__all__ = [
    "HIGH_IMPACT_DECISION_REASONS",
    "RevisionAssessment",
    "RevisionImpact",
    "RevisionReviewPlan",
    "assess_revision_impact",
    "plan_no_reply_review",
    "plan_reply_review",
]
