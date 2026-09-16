from __future__ import annotations

import pytest
from pydantic import ValidationError

from feishu_shadow_agent.evals.schemas import (
    DraftTaskSessionLabels,
    TaskSessionLabels,
    TaskSessionScenario,
)


def test_task_session_scenario_accepts_multiple_resume_targets() -> None:
    scenario = TaskSessionScenario.model_validate(
        {
            "mode": "resume",
            "setup_message_ids": ["om_1"],
            "target_message_ids": ["om_2", "om_3"],
        }
    )

    assert scenario.target_message_id is None
    assert scenario.target_message_ids == ["om_2", "om_3"]


def test_task_session_scenario_accepts_final_rebuild_for_resume() -> None:
    scenario = TaskSessionScenario.model_validate(
        {
            "mode": "resume",
            "setup_message_ids": ["om_1"],
            "target_message_id": "om_2",
            "final_rebuild": {"recent_messages": 4, "summary": "摘要"},
        }
    )

    assert scenario.final_rebuild is not None
    assert scenario.final_rebuild.recent_messages == 4
    assert scenario.final_rebuild.summary == "摘要"


def test_task_session_scenario_rejects_final_rebuild_for_initial() -> None:
    with pytest.raises(ValidationError):
        TaskSessionScenario.model_validate(
            {
                "mode": "initial",
                "message_ids": ["om_1"],
                "final_rebuild": {"recent_messages": 4},
            }
        )


@pytest.mark.parametrize(
    "targets",
    [
        {},
        {"target_message_ids": []},
        {"target_message_id": "om_2", "target_message_ids": ["om_3"]},
        {"target_message_ids": ["om_2", "om_2"]},
    ],
)
def test_task_session_scenario_rejects_invalid_resume_targets(
    targets: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TaskSessionScenario.model_validate(
            {
                "mode": "resume",
                "setup_message_ids": ["om_1"],
                **targets,
            }
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
