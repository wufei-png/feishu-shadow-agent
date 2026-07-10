from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..agent_backend import AgentBackend
from ..config import LoadedConfig
from ..paths import resolve_agent_working_dir
from ..processing import ProcessingResult, TaskProcessingService
from ..routing import (
    ROUTER_PLACEHOLDER_REASONS,
    CandidateCollector,
    MessageRouter,
    RoutingResult,
)
from .artifacts import EvalError
from .cases import LoadedEvalCase, message_sent_at
from .runtime import TrialRuntime, seed_router_scenario
from .schemas import DraftRouterLabels, RouterLabels, RouterScenario


def run_router_trial(
    *,
    case: LoadedEvalCase,
    runtime: TrialRuntime,
    loaded: LoadedConfig,
    backend: AgentBackend,
    run_id: str,
) -> dict[str, Any]:
    if not isinstance(case.scenario, RouterScenario):
        raise EvalError("router runner requires RouterScenario")
    aliases, target = seed_router_scenario(runtime=runtime, case=case, loaded=loaded)
    now = message_sent_at(case.raw_messages[case.scenario.target.message_id])
    runtime.clock.set(now)
    inserted = runtime.store.upsert_message(target)
    watch_until = _plus_minutes(now, loaded.config.lifecycle.watch_minutes)
    collector = CandidateCollector(runtime.store)
    active_candidates = collector.collect(target, now=now)
    historical = []
    id_to_alias = {task.id: alias for alias, task in aliases.items()}
    candidates = [
        {
            "task_key": id_to_alias.get(candidate.task.id),
            "task_short_id": candidate.task.short_id,
            "status": candidate.task.status,
            "matched_by": candidate.matched_by,
        }
        for candidate in active_candidates
    ]
    router = MessageRouter(
        store=runtime.store,
        collector=collector,
        closed_recall_days=loaded.config.lifecycle.closed_recall_days,
        burst_attach_seconds=loaded.config.lifecycle.burst_attach_seconds,
    )
    result = router.route(
        target,
        source=case.scenario.target.source,
        inserted=inserted,
        now=now,
        watch_until=watch_until,
        agent_working_dir=str(
            resolve_agent_working_dir(
                loaded.config.agent_backend.working_dir, loaded.base_dir
            )
        ),
    )
    if result.decision.reason == "closed_recall_router_placeholder":
        historical = runtime.store.get_related_closed_tasks(
            target,
            since=_minus_days(now, loaded.config.lifecycle.closed_recall_days),
        )
        candidates.extend(
            {
                "task_key": id_to_alias.get(task.id),
                "task_short_id": task.short_id,
                "status": task.status,
                "matched_by": "closed_recall",
            }
            for task in historical
        )
    if result.decision.reason in ROUTER_PLACEHOLDER_REASONS:
        processor = _task_processor(runtime=runtime, loaded=loaded, backend=backend)
        resolved = processor.run_task_router(
            message=target,
            source=case.scenario.target.source,
            reason=result.decision.reason or "",
            now=now,
            watch_until=watch_until,
            run_id=run_id,
        )
        if isinstance(resolved, RoutingResult):
            result = resolved
        elif isinstance(resolved, ProcessingResult) and resolved.reason in {
            "task_router_failed",
            "task_router_schema_failed",
        }:
            raise EvalError(f"router model stage failed: {resolved.reason}")
        else:
            result = _latest_routing_result(runtime)

    state = runtime.state_summary()
    actual_alias = id_to_alias.get(result.decision.target_task_id or -1)
    actual = {
        "route": result.decision.route,
        "task_key": actual_alias,
        "reason": result.decision.reason,
        "matched_by": result.decision.matched_by,
        "router_called": result.decision.router_called,
        "candidates_count": result.decision.candidates_count,
    }
    structure = _score(case.labels, actual)
    return {
        "schema_version": "router_trial_report_v1",
        "label_status": case.status,
        "target_message_id": target.message_id,
        "task_aliases": {alias: task.short_id for alias, task in aliases.items()},
        "candidates": candidates,
        "actual": actual,
        "structure": structure,
        "state": state,
        "passed": structure["passed"] if case.status == "golden" else None,
    }


def _task_processor(
    *, runtime: TrialRuntime, loaded: LoadedConfig, backend: AgentBackend
) -> TaskProcessingService:
    return TaskProcessingService(
        store=runtime.store,
        config=loaded.config,
        agent_backend=backend,
        logger=runtime.logger,
        agent_working_dir=resolve_agent_working_dir(
            loaded.config.agent_backend.working_dir, loaded.base_dir
        ),
        config_base_dir=loaded.base_dir,
        preserve_context_store_path=True,
        sleep_func=lambda _: None,
    )


def _latest_routing_result(runtime: TrialRuntime) -> RoutingResult:
    state = runtime.state_summary()
    if not state["routing"]:
        raise EvalError("router produced no routing audit")
    row = state["routing"][-1]
    from ..types import RouteDecision

    task_id = row.get("target_task_id")
    task = runtime.store.get_task_by_id(task_id) if task_id is not None else None
    return RoutingResult(
        decision=RouteDecision(
            route=row["route"],
            target_task_id=task_id,
            target_task_short_id=None if task is None else task.short_id,
            reason=row.get("route_reason"),
            candidates_count=row.get("candidates_count") or 0,
            router_called=bool(row.get("router_called")),
            matched_by=row.get("matched_by"),
        ),
        task=task,
    )


def _score(
    labels: DraftRouterLabels | RouterLabels | Any | None,
    actual: dict[str, Any],
) -> dict[str, Any]:
    if labels is None:
        return {"checked": False, "passed": None, "mismatches": []}
    expected_route = labels.route
    expected_task = labels.task_key
    mismatches: list[dict[str, Any]] = []
    if expected_route is not None and actual["route"] != expected_route:
        mismatches.append(
            {
                "field": "route",
                "expected": expected_route,
                "actual": actual["route"],
            }
        )
    if expected_task is not None and actual["task_key"] != expected_task:
        mismatches.append(
            {
                "field": "task_key",
                "expected": expected_task,
                "actual": actual["task_key"],
            }
        )
    checked = expected_route is not None or expected_task is not None
    return {
        "checked": checked,
        "passed": not mismatches if checked else None,
        "mismatches": mismatches,
    }


def _plus_minutes(value: str, minutes: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(minutes=minutes)).isoformat()


def _minus_days(value: str, days: int) -> str:
    return (datetime.fromisoformat(value) - timedelta(days=days)).isoformat()
