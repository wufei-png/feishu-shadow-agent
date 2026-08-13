from __future__ import annotations

from typing import Literal, TypeAlias

Answerability: TypeAlias = Literal["auto_reply", "needs_owner", "no_reply"]
AutoReplyDecisionReason: TypeAlias = Literal["sufficient_evidence_low_risk"]
NoReplyDecisionReason: TypeAlias = Literal[
    "no_response_needed",
    "already_resolved",
    "duplicate_or_stale",
]
NeedsOwnerDecisionReason: TypeAlias = Literal[
    "insufficient_evidence",
    "commitment_or_authorization",
    "sensitive_or_high_impact",
    "write_or_permission",
    "human_judgment_required",
]
DecisionReason: TypeAlias = (
    AutoReplyDecisionReason | NoReplyDecisionReason | NeedsOwnerDecisionReason
)

DECISION_REASONS_BY_ANSWERABILITY: dict[Answerability, frozenset[str]] = {
    "auto_reply": frozenset({"sufficient_evidence_low_risk"}),
    "no_reply": frozenset(
        {"no_response_needed", "already_resolved", "duplicate_or_stale"}
    ),
    "needs_owner": frozenset(
        {
            "insufficient_evidence",
            "commitment_or_authorization",
            "sensitive_or_high_impact",
            "write_or_permission",
            "human_judgment_required",
        }
    ),
}


def validate_decision_reason(
    answerability: Answerability, decision_reason: DecisionReason | None
) -> None:
    if answerability == "auto_reply" and decision_reason is None:
        return
    allowed = DECISION_REASONS_BY_ANSWERABILITY[answerability]
    if decision_reason is None:
        raise ValueError(f"{answerability} requires decision_reason")
    if decision_reason not in allowed:
        raise ValueError(
            f"decision_reason {decision_reason!r} is not valid for {answerability}"
        )
