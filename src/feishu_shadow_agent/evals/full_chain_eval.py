from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..agent_backend import AgentBackend
from ..config import LoadedConfig
from ..ingestion import IngestionService, MessageNormalizer
from ..paths import resolve_agent_working_dir
from ..processing import FORBIDDEN_MENTION_RE, TaskProcessingService
from ..routing import TRIGGER_SOURCES, CandidateCollector
from ..types import NormalizedMessage
from .artifacts import EvalError
from .cases import LoadedEvalCase, message_sent_at
from .judge import run_semantic_judge
from .resources import EvalResourceClient
from .runtime import TrialRuntime
from .schemas import (
    TASK_ROUTES,
    DraftFullChainLabels,
    FullChainLabels,
    FullChainScenario,
)


def run_full_chain_trial(
    *,
    case: LoadedEvalCase,
    runtime: TrialRuntime,
    loaded: LoadedConfig,
    backend: AgentBackend,
    run_id: str,
) -> dict[str, Any]:
    if not isinstance(case.scenario, FullChainScenario):
        raise EvalError("full-chain runner requires FullChainScenario")
    processor = TaskProcessingService(
        store=runtime.store,
        config=loaded.config,
        agent_backend=backend,
        logger=runtime.logger,
        agent_working_dir=resolve_agent_working_dir(
            loaded.config.agent_backend.working_dir, loaded.base_dir
        ),
        config_base_dir=loaded.base_dir,
        preserve_context_store_path=True,
    )
    ingestion = IngestionService(
        store=runtime.store,
        feishu_client=EvalResourceClient(
            case=case, resource_base_dir=runtime.access_root
        ),
        config=loaded.config,
        logger=runtime.logger,
        task_processor=processor,
        clock=runtime.clock,
        config_base_dir=loaded.base_dir,
        resource_base_dir=runtime.access_root,
        store_absolute_resource_paths=True,
        preserve_resource_base_path=True,
    )
    aliases: dict[int, str] = {}
    normalizer = MessageNormalizer(owner_open_id=loaded.config.owner.open_id)
    setup_reports: list[dict[str, Any]] = []
    for index, item in enumerate(case.scenario.setup, start=1):
        raw = case.raw_messages[item.message_id]
        runtime.clock.set(message_sent_at(raw))
        candidates = _router_candidates(
            runtime=runtime,
            message=normalizer.normalize(raw),
            source=item.source,
            aliases=aliases,
            now=message_sent_at(raw),
            closed_recall_days=loaded.config.lifecycle.closed_recall_days,
        )
        before = runtime.state_summary()
        ingestion.process_eligible_raw_message(
            raw,
            source=item.source,
            default_chat_type=None,
            run_id=f"{run_id}-setup-{index}",
        )
        after = runtime.state_summary()
        _raise_on_processing_error(after, message_id=item.message_id, stage="setup")
        _assign_new_aliases(after, aliases)
        setup_reports.append(
            _message_trace(
                message_id=item.message_id,
                before=before,
                after=after,
                aliases=aliases,
                candidates=candidates,
            )
        )

    target_item = case.scenario.target
    target_raw = case.raw_messages[target_item.message_id]
    runtime.clock.set(message_sent_at(target_raw))
    target_candidates = _router_candidates(
        runtime=runtime,
        message=normalizer.normalize(target_raw),
        source=target_item.source,
        aliases=aliases,
        now=message_sent_at(target_raw),
        closed_recall_days=loaded.config.lifecycle.closed_recall_days,
    )
    before_target = runtime.state_summary()
    ingestion.process_eligible_raw_message(
        target_raw,
        source=target_item.source,
        default_chat_type=None,
        run_id=f"{run_id}-target",
    )
    after_target = runtime.state_summary()
    _raise_on_processing_error(
        after_target, message_id=target_item.message_id, stage="target"
    )
    target_trace = _message_trace(
        message_id=target_item.message_id,
        before=before_target,
        after=after_target,
        aliases=aliases,
        candidates=target_candidates,
    )
    _assign_new_aliases(after_target, aliases)
    target_trace["task_aliases_after"] = _alias_report(after_target, aliases)

    structure = _score_structure(
        labels=case.labels,
        trace=target_trace,
        state=after_target,
        aliases=aliases,
        target_message_id=target_item.message_id,
    )
    semantic: dict[str, Any] = {"status": "not_scored"}
    reference = getattr(case.labels, "reference_answer", None)
    if structure["passed"] is not False and reference:
        candidate = str(target_trace.get("effective_reply") or "")
        judge = run_semantic_judge(
            backend=backend,
            reference_answer=reference,
            candidate_answer=candidate,
            visible_context={
                "setup": [
                    case.raw_messages[item.message_id] for item in case.scenario.setup
                ],
                "target": target_raw,
                "raw_task_session_reply": target_trace.get("raw_reply"),
            },
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
        "schema_version": "full_chain_trial_report_v1",
        "label_status": case.status,
        "setup": setup_reports,
        "target": target_trace,
        "task_aliases": _alias_report(after_target, aliases),
        "structure": structure,
        "semantic": semantic,
        "state": after_target,
        "would_send": {
            "actions": after_target["actions"],
            "approvals": after_target["approvals"],
        },
        "passed": passed,
    }


def _message_trace(
    *,
    message_id: str,
    before: dict[str, Any],
    after: dict[str, Any],
    aliases: dict[int, str],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    before_routing = len(before["routing"])
    routing_rows = [
        row
        for row in after["routing"][before_routing:]
        if row["message_id"] == message_id
    ]
    if not routing_rows:
        raise EvalError(f"full-chain message produced no routing audit: {message_id}")
    routing = routing_rows[-1]
    task_id = routing.get("target_task_id")
    audits = [
        row
        for row in after["agent_audits"][len(before["agent_audits"]) :]
        if message_id in (row.get("input_message_ids") or [])
    ]
    task_session_audits = [
        row for row in audits if row.get("request_type") == "task_session"
    ]
    raw_model = task_session_audits[-1].get("response") if task_session_audits else None
    new_actions = after["actions"][len(before["actions"]) :]
    new_approvals = after["approvals"][len(before["approvals"]) :]
    effective_reply = _effective_reply(new_actions, new_approvals)
    task_session_plan = _task_session_plan(
        message_id=message_id,
        task_id=task_id,
        before=before,
        after=after,
        audit=task_session_audits[-1] if task_session_audits else None,
    )
    return {
        "message_id": message_id,
        "routing": {
            "route": routing["route"],
            "task_key": aliases.get(task_id),
            "reason": routing.get("route_reason"),
            "router_called": bool(routing.get("router_called")),
            "matched_by": routing.get("matched_by"),
            "candidates_count": routing.get("candidates_count") or 0,
        },
        "router_candidates": candidates,
        "agent_audits": audits,
        "raw_model_json": raw_model,
        "task_session_plan": task_session_plan,
        "raw_reply": None
        if not isinstance(raw_model, dict)
        else raw_model.get("proposed_reply"),
        "effective_reply": effective_reply,
        "new_actions": new_actions,
        "new_approvals": new_approvals,
        "state_changes": _state_changes(before=before, after=after),
    }


def _router_candidates(
    *,
    runtime: TrialRuntime,
    message: NormalizedMessage,
    source: str,
    aliases: dict[int, str],
    now: str,
    closed_recall_days: int,
) -> list[dict[str, Any]]:
    active = CandidateCollector(runtime.store).collect(message, now=now)
    rows = [
        {
            "task_key": aliases.get(candidate.task.id),
            "task_short_id": candidate.task.short_id,
            "status": candidate.task.status,
            "matched_by": candidate.matched_by,
        }
        for candidate in active
    ]
    if not active and source in TRIGGER_SOURCES:
        historical = runtime.store.get_related_closed_tasks(
            message,
            since=_minus_days(now, closed_recall_days),
        )
        rows.extend(
            {
                "task_key": aliases.get(task.id),
                "task_short_id": task.short_id,
                "status": task.status,
                "matched_by": "closed_recall",
            }
            for task in historical
        )
    return rows


def _minus_days(value: str, days: int) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise EvalError(f"evaluation time must include timezone: {value}")
    return (parsed - timedelta(days=days)).isoformat()


def _effective_reply(
    actions: list[dict[str, Any]], approvals: list[dict[str, Any]]
) -> str | None:
    for action in reversed(actions):
        payload = action.get("payload")
        if action.get("kind") == "send_reply" and isinstance(payload, dict):
            text = payload.get("text")
            return text if isinstance(text, str) else None
    for approval in reversed(approvals):
        payload = approval.get("payload")
        if approval.get("kind") == "send_reply" and isinstance(payload, dict):
            text = payload.get("text")
            return text if isinstance(text, str) else None
    return None


def _task_session_plan(
    *,
    message_id: str,
    task_id: int | None,
    before: dict[str, Any],
    after: dict[str, Any],
    audit: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if task_id is None or audit is None:
        return None
    before_task = next(
        (task for task in before["tasks"] if task["id"] == task_id), None
    )
    after_task = next((task for task in after["tasks"] if task["id"] == task_id), None)
    if after_task is None:
        return None
    session_resumed = bool(before_task and before_task["has_session"])
    reply_targets = [message_id]
    if after_task.get("root_message_id"):
        reply_targets.append(after_task["root_message_id"])
    return {
        "session_resumed": session_resumed,
        "task_message_ids": after_task["message_ids"],
        "prompt_message_ids": audit.get("input_message_ids") or [],
        "reply_target_message_ids": list(dict.fromkeys(reply_targets)),
        "output_model": "FollowupTaskSessionOutput"
        if session_resumed
        else "InitialTaskSessionOutput",
    }


def _state_changes(*, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_tasks = {int(task["id"]): task for task in before["tasks"]}
    changed_tasks = [
        task for task in after["tasks"] if before_tasks.get(int(task["id"])) != task
    ]
    return {
        "tasks": changed_tasks,
        "processing": after["processing"][len(before["processing"]) :],
    }


def _score_structure(
    *,
    labels: DraftFullChainLabels | FullChainLabels | Any | None,
    trace: dict[str, Any],
    state: dict[str, Any],
    aliases: dict[int, str],
    target_message_id: str,
) -> dict[str, Any]:
    expected_router = None if labels is None else labels.router
    actual_router = trace["routing"]
    mismatches: list[dict[str, Any]] = []
    router_mismatches: list[dict[str, Any]] = []
    expected_route = None if expected_router is None else expected_router.route
    expected_task_key = None if expected_router is None else expected_router.task_key
    if expected_route is not None and actual_router["route"] != expected_route:
        router_mismatches.append(
            {
                "field": "router.route",
                "expected": expected_route,
                "actual": actual_router["route"],
            }
        )
    if expected_task_key is not None and actual_router["task_key"] != expected_task_key:
        router_mismatches.append(
            {
                "field": "router.task_key",
                "expected": expected_task_key,
                "actual": actual_router["task_key"],
            }
        )
    mismatches.extend(router_mismatches)
    router_matches = not router_mismatches
    expected_session = None if labels is None else labels.task_session
    if actual_router["route"] in TASK_ROUTES:
        raw = trace.get("raw_model_json")
        if not isinstance(raw, dict):
            mismatches.append(
                {
                    "field": "task_session",
                    "expected": "validated output",
                    "actual": None,
                }
            )
        else:
            reply_target = raw.get("reply_target_message_id")
            task = _target_task(state, trace, aliases)
            allowed_targets = [target_message_id]
            if task and task.get("root_message_id"):
                allowed_targets.append(task["root_message_id"])
            if reply_target is not None and reply_target not in allowed_targets:
                mismatches.append(
                    {
                        "field": "task_session.reply_target_message_id",
                        "expected": allowed_targets,
                        "actual": reply_target,
                    }
                )
            if (
                actual_router["route"] == "new_task"
                and not str(raw.get("task_label") or "").strip()
            ):
                mismatches.append(
                    {
                        "field": "task_session.task_label",
                        "expected": "non-empty",
                        "actual": raw.get("task_label"),
                    }
                )
            if FORBIDDEN_MENTION_RE.search(str(raw.get("proposed_reply") or "")):
                mismatches.append(
                    {
                        "field": "task_session.proposed_reply",
                        "expected": "no Feishu mentions",
                        "actual": "contains forbidden mention",
                    }
                )
            if router_matches and expected_session is not None:
                for field in ("answerability", "watch_action"):
                    expected = getattr(expected_session, field)
                    if expected is not None and raw.get(field) != expected:
                        mismatches.append(
                            {
                                "field": f"task_session.{field}",
                                "expected": expected,
                                "actual": raw.get(field),
                            }
                        )
    checked = expected_route is not None or expected_session is not None
    return {
        "checked": checked,
        "router_matches": router_matches,
        "task_session_skipped": bool(expected_route is not None and not router_matches),
        "passed": not mismatches if checked else (False if mismatches else None),
        "mismatches": mismatches,
    }


def _target_task(
    state: dict[str, Any], trace: dict[str, Any], aliases: dict[int, str]
) -> dict[str, Any] | None:
    task_key = trace["routing"].get("task_key")
    for task in state["tasks"]:
        if aliases.get(task["id"]) == task_key:
            return task
    if trace["routing"]["route"] == "new_task" and state["tasks"]:
        return state["tasks"][-1]
    return None


def _raise_on_processing_error(
    state: dict[str, Any], *, message_id: str, stage: str
) -> None:
    failures = [
        row
        for row in state["processing"]
        if row["message_id"] == message_id and row["status"] != "processed"
    ]
    if failures:
        raise EvalError(f"full-chain {stage} processing failed: {failures}")


def _assign_new_aliases(state: dict[str, Any], aliases: dict[int, str]) -> None:
    for task in state["tasks"]:
        task_id = int(task["id"])
        if task_id not in aliases:
            aliases[task_id] = f"task_{len(aliases) + 1}"


def _alias_report(state: dict[str, Any], aliases: dict[int, str]) -> dict[str, str]:
    return {
        alias: str(task["short_id"])
        for task in state["tasks"]
        if (alias := aliases.get(int(task["id"]))) is not None
    }
