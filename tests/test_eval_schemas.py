from __future__ import annotations

import pytest
from pydantic import ValidationError

from feishu_shadow_agent.evals.schemas import (
    DraftTaskSessionLabels,
    TaskSessionLabels,
)


def test_task_session_labels_default_expected_skills_for_legacy_artifacts() -> None:
    draft = DraftTaskSessionLabels.model_validate({})
    golden = TaskSessionLabels.model_validate(
        {
            "answerability": "no_reply",
            "decision_reason": "no_response_needed",
            "watch_action": "keep_watching",
        }
    )

    assert draft.expected_skills == []
    assert golden.expected_skills == []


def test_task_session_labels_normalize_expected_skills() -> None:
    labels = TaskSessionLabels.model_validate(
        {
            "answerability": "no_reply",
            "decision_reason": "already_resolved",
            "watch_action": "keep_watching",
            "expected_skills": [" docmate "],
        }
    )

    assert labels.expected_skills == ["docmate"]


@pytest.mark.parametrize(
    ("answerability", "decision_reason"),
    [
        ("auto_reply", "already_resolved"),
        ("no_reply", "insufficient_evidence"),
        ("needs_owner", "no_response_needed"),
    ],
)
def test_task_session_labels_reject_invalid_decision_reason_combination(
    answerability: str,
    decision_reason: str,
) -> None:
    with pytest.raises(ValidationError):
        TaskSessionLabels.model_validate(
            {
                "answerability": answerability,
                "decision_reason": decision_reason,
                "watch_action": "keep_watching",
            }
        )


def test_task_session_labels_allow_legacy_missing_decision_reason() -> None:
    labels = TaskSessionLabels.model_validate(
        {"answerability": "no_reply", "watch_action": "keep_watching"}
    )

    assert labels.decision_reason is None


@pytest.mark.parametrize("expected_skills", [[""], ["  "], ["docmate", "docmate"]])
def test_task_session_labels_reject_invalid_expected_skills(
    expected_skills: list[str],
) -> None:
    with pytest.raises(ValidationError):
        TaskSessionLabels.model_validate(
            {
                "answerability": "no_reply",
                "decision_reason": "no_response_needed",
                "watch_action": "keep_watching",
                "expected_skills": expected_skills,
            }
        )
