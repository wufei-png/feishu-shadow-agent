from __future__ import annotations

import fcntl
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..config import LoadedConfig
from ..ingestion import MessageNormalizer
from ..jsonl import JSONLLogger
from ..paths import resolve_agent_working_dir
from ..store.sqlite_store import SQLiteStore
from ..types import NormalizedMessage, ResourceRef, TaskRecord
from .artifacts import EvalError, evals_base_dir
from .cases import LoadedEvalCase, message_sent_at, resource_fixture_path
from .schemas import (
    ResourceFixture,
    RouterScenario,
    RouterTaskFixture,
    TaskSessionScenario,
)


class EvaluationClock:
    def __init__(self, value: str):
        self._value = value

    def __call__(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        _parse_datetime(value)
        self._value = value


@dataclass
class TrialRuntime:
    root: Path
    access_root: Path
    evidence_dir: Path
    clock: EvaluationClock
    store: SQLiteStore
    logger: JSONLLogger
    resources_dir: Path
    _temporary: tempfile.TemporaryDirectory[str]
    _lock_handle: Any

    @classmethod
    def create(
        cls,
        *,
        loaded: LoadedConfig,
        evidence_dir: Path,
        initial_time: str,
        slot_key: str,
        log_level: Literal["debug", "info", "warning", "error"] = "debug",
    ) -> TrialRuntime:
        if not slot_key or any(char not in "0123456789abcdef" for char in slot_key):
            raise EvalError("trial slot key must be lowercase hexadecimal")
        evidence_dir.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="feishu-shadow-eval-")
        root = Path(temporary.name)
        slot = evals_base_dir(loaded) / ".trial-slots" / slot_key
        slot.mkdir(parents=True, exist_ok=True)
        lock_handle = (slot / "slot.lock").open("a+", encoding="utf-8")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        access_root = slot / "current"
        try:
            _remove_access_root(access_root)
            access_root.symlink_to(root, target_is_directory=True)
            clock = EvaluationClock(initial_time)
            store = SQLiteStore(access_root / "eval.sqlite3", clock=clock)
            store.migrate()
            store.import_product_policy_from_config(
                loaded.config,
                replace=True,
                actor="evaluation",
                reason="deterministic trial baseline",
            )
            resources_dir = access_root / "resources"
            resources_dir.mkdir(parents=True, exist_ok=True)
            logger = JSONLLogger(evidence_dir / "events.jsonl", level=log_level)
            return cls(
                root=root,
                access_root=access_root,
                evidence_dir=evidence_dir,
                clock=clock,
                store=store,
                logger=logger,
                resources_dir=resources_dir,
                _temporary=temporary,
                _lock_handle=lock_handle,
            )
        except Exception:
            try:
                try:
                    _remove_access_root(access_root)
                finally:
                    temporary.cleanup()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
            raise

    def close(self) -> None:
        try:
            try:
                _remove_access_root(self.access_root)
            finally:
                self._temporary.cleanup()
        finally:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()

    def state_summary(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            tasks = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, short_id, status, chat_id, thread_id, root_message_id,
                           task_label, watch_until, agent_session_provider,
                           CASE WHEN agent_session_id IS NULL THEN 0 ELSE 1 END AS has_session
                    FROM tasks ORDER BY id
                    """
                ).fetchall()
            ]
            task_messages: dict[int, list[str]] = {}
            for row in conn.execute(
                """
                SELECT task_id, message_id
                FROM task_messages
                ORDER BY task_id, created_at, message_id
                """
            ).fetchall():
                task_messages.setdefault(int(row["task_id"]), []).append(
                    str(row["message_id"])
                )
            for task in tasks:
                task["message_ids"] = task_messages.get(int(task["id"]), [])
            routing = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT message_id, route, target_task_id, route_reason,
                           candidates_count, router_called, matched_by
                    FROM routing_audits ORDER BY id
                    """
                ).fetchall()
            ]
            processing = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT message_id, task_id, stage, status, attempt_count,
                           terminal_reason
                    FROM message_processing ORDER BY id
                    """
                ).fetchall()
            ]
            agent_audits = [
                _json_columns(
                    dict(row),
                    "input_message_ids_json",
                    "input_resource_ids_json",
                    "response_json",
                )
                for row in conn.execute(
                    """
                    SELECT backend_provider, request_type, task_id, agent_session_id,
                           input_message_ids_json, input_resource_ids_json,
                           response_json, error, tool_permissions_profile
                    FROM agent_audits ORDER BY id
                    """
                ).fetchall()
            ]
            approvals = [
                _json_columns(dict(row), "payload_json")
                for row in conn.execute(
                    """
                    SELECT id, short_id, task_id, kind, status, preview,
                           payload_json
                    FROM approvals ORDER BY id
                    """
                ).fetchall()
            ]
            actions = [
                _json_columns(dict(row), "payload_json", "result_json")
                for row in conn.execute(
                    """
                    SELECT id, task_id, approval_id, kind, status,
                           target_message_id, payload_json, result_json
                    FROM actions ORDER BY id
                    """
                ).fetchall()
            ]
            resources = [
                _json_columns(dict(row), "raw_json")
                for row in conn.execute(
                    """
                    SELECT message_id, file_key, resource_type, download_status,
                           path, sha256, raw_json
                    FROM resources ORDER BY id
                    """
                ).fetchall()
            ]
        return {
            "tasks": tasks,
            "routing": routing,
            "processing": processing,
            "agent_audits": agent_audits,
            "approvals": approvals,
            "actions": actions,
            "resources": resources,
        }


def seed_router_scenario(
    *, runtime: TrialRuntime, case: LoadedEvalCase, loaded: LoadedConfig
) -> tuple[dict[str, TaskRecord], NormalizedMessage]:
    scenario = _router_scenario(case)
    normalizer = MessageNormalizer(owner_open_id=loaded.config.owner.open_id)
    aliases: dict[str, TaskRecord] = {}
    target = normalizer.normalize(case.raw_messages[scenario.target.message_id])
    target_time = message_sent_at(case.raw_messages[scenario.target.message_id])
    for alias, fixture in scenario.tasks.items():
        task = _seed_task_fixture(
            runtime=runtime,
            case=case,
            loaded=loaded,
            normalizer=normalizer,
            fixture=fixture,
        )
        if fixture.status == "watching" and not (
            task.watch_until
            and _parse_datetime(task.watch_until) > _parse_datetime(target_time)
        ):
            raise EvalError(f"watching task fixture is expired at target: {alias}")
        aliases[alias] = task
    runtime.clock.set(target_time)
    return aliases, target


def seed_task_session_scenario(
    *, runtime: TrialRuntime, case: LoadedEvalCase, loaded: LoadedConfig
) -> tuple[TaskRecord, NormalizedMessage, list[NormalizedMessage]]:
    scenario = _task_session_scenario(case)
    message_ids = (
        list(scenario.message_ids or [])
        if scenario.mode == "initial"
        else list(scenario.setup_message_ids or [])
    )
    normalizer = MessageNormalizer(owner_open_id=loaded.config.owner.open_id)
    messages = [normalizer.normalize(case.raw_messages[item]) for item in message_ids]
    task = _seed_task_messages(
        runtime=runtime,
        loaded=loaded,
        messages=messages,
        task_label=None,
    )
    _seed_resource_rows(runtime=runtime, case=case, message_ids=message_ids)
    return task, messages[-1], messages


def attach_task_session_target(
    *,
    runtime: TrialRuntime,
    case: LoadedEvalCase,
    loaded: LoadedConfig,
    task: TaskRecord,
) -> NormalizedMessage:
    scenario = _task_session_scenario(case)
    if scenario.mode != "resume" or not scenario.target_message_id:
        raise EvalError("attach_task_session_target requires resume mode")
    normalizer = MessageNormalizer(owner_open_id=loaded.config.owner.open_id)
    message = normalizer.normalize(case.raw_messages[scenario.target_message_id])
    runtime.clock.set(_message_time(message))
    runtime.store.upsert_message(message)
    runtime.store.attach_message_to_task(
        task.id,
        message,
        watch_until=_watch_until(
            _message_time(message), loaded.config.lifecycle.watch_minutes
        ),
    )
    runtime.store.update_task_after_agent(
        task_id=task.id,
        status="watching",
        watch_until=_watch_until(
            _message_time(message), loaded.config.lifecycle.watch_minutes
        ),
    )
    _seed_resource_rows(
        runtime=runtime, case=case, message_ids=[scenario.target_message_id]
    )
    return message


def copy_trial_resource(
    *, runtime: TrialRuntime, case: LoadedEvalCase, fixture: ResourceFixture
) -> Path:
    source = resource_fixture_path(case.directory, fixture)
    relative = source.relative_to(case.directory / "resources")
    destination = runtime.resources_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _seed_task_fixture(
    *,
    runtime: TrialRuntime,
    case: LoadedEvalCase,
    loaded: LoadedConfig,
    normalizer: MessageNormalizer,
    fixture: RouterTaskFixture,
) -> TaskRecord:
    messages = [
        normalizer.normalize(case.raw_messages[item]) for item in fixture.message_ids
    ]
    task = _seed_task_messages(
        runtime=runtime,
        loaded=loaded,
        messages=messages,
        task_label=fixture.task_label,
    )
    if fixture.status != "watching":
        runtime.clock.set(_message_time(messages[-1]))
        runtime.store.update_task_after_agent(
            task_id=task.id,
            status=fixture.status,
        )
        task = runtime.store.get_task_by_id(task.id)
    return task


def _seed_task_messages(
    *,
    runtime: TrialRuntime,
    loaded: LoadedConfig,
    messages: list[NormalizedMessage],
    task_label: str | None,
) -> TaskRecord:
    if not messages:
        raise EvalError("cannot seed a task without messages")
    storage_times = _task_message_storage_times(messages)
    first = messages[0]
    runtime.clock.set(storage_times[0])
    runtime.store.upsert_message(first)
    task = runtime.store.create_task_for_message(
        first,
        watch_until=_watch_until(
            _message_time(first), loaded.config.lifecycle.watch_minutes
        ),
        task_label=task_label,
        agent_working_dir=str(
            resolve_agent_working_dir(
                loaded.config.agent_backend.working_dir, loaded.base_dir
            )
        ),
    )
    for message, storage_time in zip(messages[1:], storage_times[1:], strict=True):
        runtime.clock.set(storage_time)
        runtime.store.upsert_message(message)
        runtime.store.attach_message_to_task(
            task.id,
            message,
            watch_until=_watch_until(
                _message_time(message), loaded.config.lifecycle.watch_minutes
            ),
        )
    return runtime.store.get_task_by_id(task.id)


def _task_message_storage_times(messages: list[NormalizedMessage]) -> list[str]:
    values: list[str] = []
    previous: datetime | None = None
    for message in messages:
        current = _parse_datetime(_message_time(message))
        if previous is not None and current <= previous:
            current = previous + timedelta(microseconds=1)
        values.append(current.isoformat())
        previous = current
    return values


def _seed_resource_rows(
    *, runtime: TrialRuntime, case: LoadedEvalCase, message_ids: list[str]
) -> None:
    selected = set(message_ids)
    for fixture in getattr(case.scenario, "resources", []):
        if fixture.message_id not in selected:
            continue
        destination = copy_trial_resource(runtime=runtime, case=case, fixture=fixture)
        runtime.store.upsert_resource(
            ResourceRef(
                message_id=fixture.message_id,
                file_key=fixture.file_key,
                resource_type=fixture.resource_type,
            ),
            download_status="downloaded",
            path=str(destination),
            sha256_hex=fixture.sha256,
        )


def _router_scenario(case: LoadedEvalCase) -> RouterScenario:
    if not isinstance(case.scenario, RouterScenario):
        raise EvalError("router runner requires RouterScenario")
    return case.scenario


def _task_session_scenario(case: LoadedEvalCase) -> TaskSessionScenario:
    if not isinstance(case.scenario, TaskSessionScenario):
        raise EvalError("task-session runner requires TaskSessionScenario")
    return case.scenario


def _message_time(message: NormalizedMessage) -> str:
    if not message.sent_at:
        raise EvalError(f"message is missing sent_at: {message.message_id}")
    _parse_datetime(message.sent_at)
    return message.sent_at


def _watch_until(value: str, minutes: int) -> str:
    return (_parse_datetime(value) + timedelta(minutes=minutes)).isoformat()


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvalError(f"invalid evaluation time: {value}") from exc
    if parsed.utcoffset() is None:
        raise EvalError(f"evaluation time must include timezone: {value}")
    return parsed


def _json_columns(row: dict[str, Any], *columns: str) -> dict[str, Any]:
    import json

    for column in columns:
        value = row.pop(column, None)
        key = column.removesuffix("_json")
        row[key] = None if value is None else json.loads(value)
    return row


def _remove_access_root(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)
