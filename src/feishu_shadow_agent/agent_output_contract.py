from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .decision import (
    DECISION_REASONS_BY_ANSWERABILITY,
    Answerability,
    DecisionReason,
    validate_decision_reason,
)


class StrictModel(BaseModel):
    """Base model for the structured agent-output boundary."""

    # Agent output is an API boundary. Unknown fields are rejected so prompt
    # drift becomes an auditable owner path instead of silently changing policy.
    model_config = ConfigDict(extra="forbid")


class TaskRouterOutput(StrictModel):
    route: Literal["new_task", "attach_task", "reopen_task", "ignore", "ambiguous"] = (
        Field(
            description=(
                "Choose exactly one route for the incoming message. "
                "new_task when the message starts an independent task. "
                "attach_task only when it clearly continues one active candidate. "
                "reopen_task only when it clearly resumes one historical closed candidate. "
                "ignore for self/owner/admin/noise messages that should not create work. "
                "ambiguous when evidence is weak, multiple candidates fit, or the target is unclear."
            )
        )
    )
    target_task_id: str | None = Field(
        default=None,
        description=(
            "Candidate task_id to act on. Required for attach_task and reopen_task; it must exactly "
            "match a task_id from the provided candidates and must not be invented. Must be null for new_task, "
            "ignore, and ambiguous."
        ),
    )
    reason: str = Field(
        default="", description="Short operator-readable reason for the decision."
    )

    @model_validator(mode="after")
    def validate_target_for_route(self) -> TaskRouterOutput:
        if self.route in {"attach_task", "reopen_task"}:
            if self.target_task_id is None or not self.target_task_id.strip():
                raise ValueError(f"{self.route} requires a non-empty target_task_id")
        elif self.target_task_id is not None:
            raise ValueError(f"{self.route} requires target_task_id to be null")
        return self


class BaseTaskSessionOutput(StrictModel):
    prompt_contract_rules: ClassVar[tuple[tuple[str, str], ...]] = (
        (
            "answerability",
            "`auto_reply` only for sufficient low-risk evidence; `needs_owner` for uncertainty, commitments, "
            "privacy, writes or permission expansion, or unclear human responsibility; `no_reply` when no external "
            "reply is needed.",
        ),
        (
            "decision_reason",
            "for `needs_owner`, one of the allowed uncertainty, commitment, sensitive, write, or human-judgment "
            "reasons; for `no_reply`, one of the allowed no-response, resolved, or stale reasons; for `auto_reply`, "
            "null or `sufficient_evidence_low_risk`.",
        ),
        (
            "proposed_reply",
            "non-empty plain reply text for `auto_reply` or `needs_owner`; empty for `no_reply`.",
        ),
        (
            "reply_target_message_id",
            "one allowed Reply Context target for `auto_reply` or `needs_owner`; null for `no_reply`.",
        ),
        ("watch_action", "`keep_watching` or `close`."),
    )

    answerability: Answerability = Field(
        description=(
            "Whether the daemon may reply automatically, needs owner review, or should not reply. Use auto_reply "
            "only for sufficient evidence and low-risk replies; use needs_owner for uncertainty, commitments, "
            "privacy-sensitive content, writes or permission expansion, or unclear human responsibility."
        )
    )
    decision_reason: DecisionReason | None = Field(
        description=(
            "Primary reason for the answerability decision. Required for needs_owner and no_reply. "
            "For auto_reply it may be null; when present it must be sufficient_evidence_low_risk."
        )
    )
    proposed_reply: str = Field(
        default="",
        description=(
            "Plain reply text without Feishu @ mentions. Required and non-empty for "
            "auto_reply and needs_owner; must be empty for no_reply."
        ),
    )
    reply_target_message_id: str | None = Field(
        default=None,
        description=(
            "Message id to reply to. Required for auto_reply and needs_owner; must be null "
            "for no_reply. When present, it must be one of the allowed reply targets in Reply Context."
        ),
    )
    watch_action: Literal["keep_watching", "close"] = Field(
        default="keep_watching",
        description="Whether to keep watching this task or close it.",
    )

    @model_validator(mode="after")
    def validate_reply_fields_for_answerability(self) -> BaseTaskSessionOutput:
        validate_decision_reason(self.answerability, self.decision_reason)
        proposed_reply = self.proposed_reply.strip()
        reply_target = (
            None
            if self.reply_target_message_id is None
            else self.reply_target_message_id.strip()
        )
        if self.answerability == "no_reply":
            if proposed_reply:
                raise ValueError("no_reply requires proposed_reply to be empty")
            if self.reply_target_message_id is not None:
                raise ValueError("no_reply requires reply_target_message_id to be null")
            return self
        if not proposed_reply:
            raise ValueError(
                f"{self.answerability} requires a non-empty proposed_reply"
            )
        if not reply_target:
            raise ValueError(
                f"{self.answerability} requires a non-empty reply_target_message_id"
            )
        return self


class InitialTaskSessionOutput(BaseTaskSessionOutput):
    task_label: str = Field(
        description="Short task label for operator status views, based on the initial task."
    )

    @field_validator("task_label")
    @classmethod
    def trim_label(cls, value: str) -> str:
        return " ".join(value.split())[:100]


class FollowupTaskSessionOutput(BaseTaskSessionOutput):
    pass


class ReplyPostprocessOutput(StrictModel):
    status: Literal["ok", "needs_owner"] = Field(
        description="ok when final_reply is safe to use; otherwise needs_owner."
    )
    final_reply: str = Field(
        default="", description="Postprocessed reply text without Feishu @ mentions."
    )


class OwnerStyleRefreshOutput(StrictModel):
    status: Literal["ok", "failed"] = Field(
        description="ok when profile_markdown is ready to write."
    )
    profile_markdown: str = Field(
        default="", description="Generated Markdown owner style profile."
    )


def task_session_output_contract(output_model: type[BaseTaskSessionOutput]) -> str:
    """Render the compact contract from the canonical output model metadata."""

    field_names = output_model.model_fields
    lines = ["Return exactly one final JSON object with no extra fields:"]
    for field_name, rule in BaseTaskSessionOutput.prompt_contract_rules:
        if field_name == "decision_reason":
            rule = _decision_reason_contract()
        if field_name in field_names:
            lines.append(f"- `{field_name}`: {rule}")
    if "task_label" in field_names:
        lines.append("- `task_label`: a short label for the initial task.")
    lines.append(
        "Do not include Markdown, explanatory text, or @ mentions in the final response."
    )
    return "\n".join(lines)


def _decision_reason_contract() -> str:
    allowed_reasons = {
        "needs_owner": ", ".join(
            f"`{reason}`"
            for reason in (
                "insufficient_evidence",
                "commitment_or_authorization",
                "sensitive_or_high_impact",
                "write_or_permission",
                "human_judgment_required",
            )
            if reason in DECISION_REASONS_BY_ANSWERABILITY["needs_owner"]
        ),
        "no_reply": ", ".join(
            f"`{reason}`"
            for reason in (
                "no_response_needed",
                "already_resolved",
                "duplicate_or_stale",
            )
            if reason in DECISION_REASONS_BY_ANSWERABILITY["no_reply"]
        ),
        "auto_reply": ", ".join(
            f"`{reason}`"
            for reason in ("sufficient_evidence_low_risk",)
            if reason in DECISION_REASONS_BY_ANSWERABILITY["auto_reply"]
        ),
    }
    return (
        "for `needs_owner`, one of "
        f"{allowed_reasons['needs_owner']}; for `no_reply`, one of "
        f"{allowed_reasons['no_reply']}; for `auto_reply`, null or "
        f"{allowed_reasons['auto_reply']}."
    )


__all__ = [
    "BaseTaskSessionOutput",
    "FollowupTaskSessionOutput",
    "InitialTaskSessionOutput",
    "OwnerStyleRefreshOutput",
    "ReplyPostprocessOutput",
    "StrictModel",
    "TaskRouterOutput",
    "task_session_output_contract",
]
