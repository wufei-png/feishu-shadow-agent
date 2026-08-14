from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..agent_backend import StructuredOutputBackend
from .artifacts import EvalError, text_sha256
from .schemas import SemanticJudgeOutput


@dataclass(frozen=True)
class SemanticJudgeResult:
    output: SemanticJudgeOutput
    prompt: str

    @property
    def passed(self) -> bool:
        return self.output.verdict == "pass"

    def report(self) -> dict[str, Any]:
        return {
            **self.output.model_dump(mode="json"),
            "passed": self.passed,
            "prompt_hash": text_sha256(self.prompt),
        }


def run_semantic_judge(
    *,
    backend: StructuredOutputBackend,
    reference_answer: str,
    candidate_answer: str,
    visible_context: dict[str, Any],
    cwd: str | Path | None,
) -> SemanticJudgeResult:
    prompt = build_semantic_judge_prompt(
        reference_answer=reference_answer,
        candidate_answer=candidate_answer,
        visible_context=visible_context,
    )
    result = backend.structured_output(
        prompt,
        output_model=SemanticJudgeOutput,
        session_id=None,
        cwd=cwd,
    )
    if not result.ok:
        detail = result.error or result.stderr or result.stdout or "judge failed"
        raise EvalError(f"semantic judge failed: {detail}")
    data = result.json_data
    if data is None and result.stdout:
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise EvalError(f"semantic judge returned invalid JSON: {exc}") from exc
    try:
        output = SemanticJudgeOutput.model_validate(data)
    except ValidationError as exc:
        raise EvalError(f"semantic judge output schema was invalid: {exc}") from exc
    return SemanticJudgeResult(output=output, prompt=prompt)


def build_semantic_judge_prompt(
    *,
    reference_answer: str,
    candidate_answer: str,
    visible_context: dict[str, Any],
) -> str:
    payload: dict[str, Any] = {
        "instruction": (
            "Compare the candidate answer with the reference answer using only factual "
            "consistency and task completion. Ignore tone and wording differences. Report "
            "only omissions, unsupported additions, contradictions, or overcommitments. "
            "Use pass only when there is no substantive difference. Use partial when the "
            "correct core remains usable but a substantive minor or major difference exists. "
            "Use fail when the core answer is missing or contradicted, or any difference is "
            "critical; a critical difference always requires fail. "
            "Return strict JSON matching output_schema."
        ),
        "reference_answer": reference_answer,
        "candidate_answer": candidate_answer,
        "visible_context": visible_context,
        "output_schema": SemanticJudgeOutput.model_json_schema(),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)
