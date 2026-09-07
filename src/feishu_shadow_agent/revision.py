from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .decision import DecisionReason

RevisionImpact = Literal["none", "low", "high", "uncertain"]

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


def assess_revision_impact(
    *,
    previous_reply: str,
    current_reply: str,
    answerability: str,
    decision_reason: DecisionReason | None,
    current_target_message_id: str | None,
    previous_target_message_id: str | None,
    revision_signals: Iterable[str] = (),
) -> RevisionAssessment:
    """Classify a correction review; agent signals can only escalate risk."""

    reply_changed = _canonical_reply(previous_reply) != _canonical_reply(current_reply)
    target_changed = current_target_message_id != previous_target_message_id
    signals = {str(signal) for signal in revision_signals}
    reasons: list[str] = []
    if reply_changed:
        reasons.append("reply_changed")
    if target_changed:
        reasons.append("reply_target_changed")
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
        and not decision_changed
        and not high_signals
    ):
        return RevisionAssessment("none", tuple(reasons), reply_changed=False)
    if (
        high_signals
        or answerability == "needs_owner"
        or decision_reason in HIGH_IMPACT_DECISION_REASONS
        or target_changed
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
    "assess_revision_impact",
]
