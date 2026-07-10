from __future__ import annotations

from pathlib import Path

import pytest

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.evals.artifacts import EvalError
from feishu_shadow_agent.evals.judge import run_semantic_judge


class JudgeBackend:
    provider = "test"

    def __init__(self, output: dict):
        self.output = output
        self.session_ids: list[str | None] = []

    def structured_output(self, prompt, *, output_model, session_id=None, cwd=None):
        self.session_ids.append(session_id)
        return AgentRunResult(
            argv=["judge"],
            exit_code=0,
            json_data=self.output,
            backend_provider=self.provider,
        )


def test_semantic_judge_uses_fresh_context_and_strict_schema() -> None:
    backend = JudgeBackend({"verdict": "pass", "differences": []})

    result = run_semantic_judge(
        backend=backend,
        reference_answer="事实 A",
        candidate_answer="事实 A",
        visible_context={"messages": []},
        cwd=Path("."),
    )

    assert result.passed is True
    assert backend.session_ids == [None]


def test_semantic_judge_invalid_contract_is_runtime_error() -> None:
    backend = JudgeBackend(
        {
            "verdict": "pass",
            "differences": [
                {
                    "type": "omission",
                    "severity": "minor",
                    "summary": "missing",
                }
            ],
        }
    )

    with pytest.raises(EvalError, match="schema was invalid"):
        run_semantic_judge(
            backend=backend,
            reference_answer="事实 A",
            candidate_answer="",
            visible_context={},
            cwd=None,
        )
