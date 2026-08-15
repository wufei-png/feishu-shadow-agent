from __future__ import annotations

from typing import Any, cast

from ..agent_backend import AgentBackend
from ..agent_invocation import AgentInvoker
from ..config import LoadedConfig
from ..context_access import ContextAccessBuilder
from ..paths import resolve_agent_working_dir
from ..processing import FORBIDDEN_MENTION_RE
from ..prompt import InitialTaskSessionOutput
from ..prompt_identity import identify_prompt
from ..task_session_runner import TaskSessionRunner, TaskSessionRunResult
from ..types import TaskRecord
from .artifacts import EvalError
from .cases import LoadedEvalCase
from .judge import run_semantic_judge
from .runtime import (
    TrialRuntime,
    attach_task_session_target,
    seed_task_session_scenario,
)
from .schemas import (
    DraftTaskSessionLabels,
    TaskSessionLabels,
    TaskSessionScenario,
)


def run_task_session_trial(
    *,
    case: LoadedEvalCase,
    runtime: TrialRuntime,
    loaded: LoadedConfig,
    backend: AgentBackend,
    run_id: str,
) -> dict[str, Any]:
    if not isinstance(case.scenario, TaskSessionScenario):
        raise EvalError("task-session runner requires TaskSessionScenario")
    task, current, _ = seed_task_session_scenario(
        runtime=runtime, case=case, loaded=loaded
    )
    runner = TaskSessionRunner(
        store=runtime.store,
        agent_backend=backend,
        agent_invoker=AgentInvoker(
            logger=runtime.logger,
            max_attempts=loaded.config.agent_backend.max_attempts,
        ),
        context_access=ContextAccessBuilder(
            store=runtime.store,
            config=loaded.config,
            preserve_store_path=True,
        ),
    )
    setup_report: dict[str, Any] | None = None
    if case.scenario.mode == "resume":
        setup_run = _run_turn(
            runner=runner,
            runtime=runtime,
            loaded=loaded,
            backend=backend,
            task=task,
            current=current,
            run_id=f"{run_id}-setup",
        )
        _require_valid_run(setup_run, stage="task-session setup")
        if setup_run.result is None or not setup_run.result.session_id:
            raise EvalError(
                "task-session setup did not return a provider session id; resume cannot continue"
            )
        runtime.store.set_task_agent_session_id(
            task.id,
            setup_run.result.session_id,
            backend_provider=str(backend.provider),
        )
        setup_output = setup_run.output
        if not isinstance(setup_output, InitialTaskSessionOutput):
            raise EvalError("task-session setup did not produce initial output")
        runtime.store.update_task_after_agent(
            task_id=task.id,
            task_label=setup_output.task_label,
            status="closed" if setup_output.watch_action == "close" else "watching",
            watch_until=task.watch_until
            if setup_output.watch_action == "keep_watching"
            else None,
        )
        setup_report = _turn_report(setup_run, current.message_id)
        current = attach_task_session_target(
            runtime=runtime, case=case, loaded=loaded, task=task
        )
        task = runtime.store.get_task_by_id(task.id)

    target_run = _run_turn(
        runner=runner,
        runtime=runtime,
        loaded=loaded,
        backend=backend,
        task=task,
        current=current,
        run_id=f"{run_id}-target",
    )
    _require_valid_run(target_run, stage="task-session target")
    if target_run.result and target_run.result.session_id:
        runtime.store.set_task_agent_session_id(
            task.id,
            target_run.result.session_id,
            backend_provider=str(backend.provider),
        )
    target_report = _turn_report(target_run, current.message_id)
    structure = _score_structure(case.labels, target_run)
    semantic: dict[str, Any] = {"status": "not_scored"}
    reference = getattr(case.labels, "reference_answer", None)
    if structure["passed"] is not False and reference:
        output = target_run.output
        if output is None:
            raise EvalError(
                "task-session target output is required for semantic judging"
            )
        judge = run_semantic_judge(
            backend=backend,
            reference_answer=reference,
            candidate_answer=output.proposed_reply,
            visible_context=_judge_context(
                case=case,
                setup_report=setup_report,
                target_message_id=current.message_id,
            ),
            cwd=resolve_agent_working_dir(
                loaded.config.agent_backend.working_dir, loaded.base_dir
            ),
        )
        semantic = {"status": "scored", **judge.report()}
    passed: bool | None = None
    if case.status == "golden":
        passed = bool(structure["passed"]) and (
            semantic.get("passed") is True if semantic["status"] == "scored" else True
        )
    return {
        "schema_version": "task_session_trial_report_v1",
        "label_status": case.status,
        "mode": case.scenario.mode,
        "setup": setup_report,
        "target": target_report,
        "structure": structure,
        "semantic": semantic,
        "state": runtime.state_summary(),
        "passed": passed,
    }


def _run_turn(
    *,
    runner: TaskSessionRunner,
    runtime: TrialRuntime,
    loaded: LoadedConfig,
    backend: AgentBackend,
    task: TaskRecord,
    current: Any,
    run_id: str,
) -> TaskSessionRunResult:
    plan = runner.build_plan(task=task, message=current)
    resources = runtime.store.list_resources_for_messages(plan.prompt_message_ids)
    result = runner.run(
        task=task,
        message=current,
        plan=plan,
        resources=resources,
        run_id=run_id,
        cwd=resolve_agent_working_dir(
            loaded.config.agent_backend.working_dir, loaded.base_dir
        ),
    )
    json_data = result.result.json_data if result.result is not None else None
    response_data = (
        cast(dict[str, Any], json_data) if isinstance(json_data, dict) else None
    )
    prompt_identity = identify_prompt("task_session", result.prompt)
    runtime.store.record_agent_audit(
        backend_provider=str(backend.provider),
        request_type="task_session",
        task_id=task.id,
        agent_session_id=None
        if result.result is None
        else result.result.session_id or plan.session_id,
        input_message_ids=plan.prompt_message_ids,
        input_resource_ids=[row["file_key"] for row in resources],
        response=response_data,
        error=result.outcome.last_error
        if result.result is None
        else result.result.error,
        latency_ms=None if result.result is None else result.result.latency_ms,
        prompt_version=prompt_identity.version,
        prompt_hash=prompt_identity.sha256,
        prompt={"text": result.prompt}
        if loaded.config.debug.save_full_agent_io
        else None,
        tool_permissions_profile=loaded.config.tool_permissions,
    )
    return result


def _require_valid_run(result: TaskSessionRunResult, *, stage: str) -> None:
    if result.result is None or not result.result.ok:
        detail = result.outcome.last_error or (
            None if result.result is None else result.result.error
        )
        raise EvalError(f"{stage} backend failed: {detail or 'unknown error'}")
    if result.validation_error is not None:
        raise EvalError(f"{stage} output schema was invalid: {result.validation_error}")
    if result.output is None:
        raise EvalError(f"{stage} returned no validated output")


def _turn_report(
    result: TaskSessionRunResult, current_message_id: str
) -> dict[str, Any]:
    return {
        "current_message_id": current_message_id,
        "plan": {
            "session_resumed": result.plan.session_id is not None,
            "task_message_ids": result.plan.task_message_ids,
            "prompt_message_ids": result.plan.prompt_message_ids,
            "reply_target_message_ids": result.plan.reply_target_message_ids,
            "output_model": result.plan.output_model.__name__,
        },
        "session_id_returned": bool(result.result and result.result.session_id),
        "raw_model_json": None if result.result is None else result.result.json_data,
        "output": None
        if result.output is None
        else result.output.model_dump(mode="json"),
    }


def _score_structure(
    labels: DraftTaskSessionLabels | TaskSessionLabels | Any | None,
    result: TaskSessionRunResult,
) -> dict[str, Any]:
    output = result.output
    if output is None:
        raise EvalError("cannot score missing task-session output")
    mismatches: list[dict[str, Any]] = []
    expected_answerability = getattr(labels, "answerability", None)
    expected_decision_reason = getattr(labels, "decision_reason", None)
    expected_watch = getattr(labels, "watch_action", None)
    if (
        expected_answerability is not None
        and output.answerability != expected_answerability
    ):
        mismatches.append(
            {
                "field": "answerability",
                "expected": expected_answerability,
                "actual": output.answerability,
            }
        )
    if expected_watch is not None and output.watch_action != expected_watch:
        mismatches.append(
            {
                "field": "watch_action",
                "expected": expected_watch,
                "actual": output.watch_action,
            }
        )
    if (
        expected_decision_reason is not None
        and output.decision_reason != expected_decision_reason
    ):
        mismatches.append(
            {
                "field": "decision_reason",
                "expected": expected_decision_reason,
                "actual": output.decision_reason,
            }
        )
    if (
        output.reply_target_message_id is not None
        and output.reply_target_message_id not in result.plan.reply_target_message_ids
    ):
        mismatches.append(
            {
                "field": "reply_target_message_id",
                "expected": result.plan.reply_target_message_ids,
                "actual": output.reply_target_message_id,
            }
        )
    if isinstance(output, InitialTaskSessionOutput) and not output.task_label.strip():
        mismatches.append(
            {"field": "task_label", "expected": "non-empty", "actual": ""}
        )
    if FORBIDDEN_MENTION_RE.search(output.proposed_reply):
        mismatches.append(
            {
                "field": "proposed_reply",
                "expected": "no Feishu mentions",
                "actual": "contains forbidden mention",
            }
        )
    checked = labels is not None and (
        expected_answerability is not None
        or expected_decision_reason is not None
        or expected_watch is not None
    )
    return {
        "checked": checked,
        "passed": not mismatches if checked else (False if mismatches else None),
        "mismatches": mismatches,
    }


def _judge_context(
    *,
    case: LoadedEvalCase,
    setup_report: dict[str, Any] | None,
    target_message_id: str,
) -> dict[str, Any]:
    scenario = case.scenario
    if not isinstance(scenario, TaskSessionScenario):
        raise EvalError("task-session judge context requires TaskSessionScenario")
    if scenario.mode == "initial":
        return {
            "task_messages": [
                case.raw_messages[item] for item in scenario.message_ids or []
            ]
        }
    setup_ids = list(scenario.setup_message_ids or [])
    return {
        "setup_messages": [case.raw_messages[item] for item in setup_ids],
        "setup_model_reply": None
        if setup_report is None
        else setup_report.get("output", {}).get("proposed_reply"),
        "target_message": case.raw_messages[target_message_id],
    }
