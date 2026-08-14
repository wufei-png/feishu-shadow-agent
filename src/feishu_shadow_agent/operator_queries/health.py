from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from ..policy import ProductPolicyInvalidError, ProductPolicyMissingError
from ..store.sqlite_store import (
    LATEST_NON_OK_HEALTH_CHECKS_SQL,
    RUN_HEARTBEAT_STALE_AFTER_SECONDS,
    SQLiteStore,
)
from ..time_utils import shift_instant
from ..types import ActionStatus
from .common import (
    ReadStoreUnavailable,
    action_dto,
    coerce_limit,
    has_core_schema,
    json_row_dict,
    parse_datetime_or_none,
    row_dict,
)

_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w:])/(?:[^\s'\"`]+)")
_SEVERITY_ORDER = {
    "info": 0,
    "warning": 1,
    "error": 2,
    "critical": 3,
}
_RUN_RUNTIME_COLUMNS = """
run_id, started_at, finished_at, status, dry_run, last_heartbeat_at,
last_tick_started_at, last_tick_finished_at, last_tick_status
"""


class HealthQuery:
    """Read-only query slice for operator health and runtime issue views."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        connect: Callable[[], sqlite3.Connection],
        now: Callable[[], str],
        policy_status: Callable[[], dict[str, Any]],
        validate_policy_store: Callable[[], None],
    ):
        self.store = store
        self._connect = connect
        self._now = now
        self._policy_status = policy_status
        self._validate_policy_store = validate_policy_store

    def health_issues(
        self,
        *,
        limit: int = 20,
        stale_after_seconds: int = 900,
        daemon_stale_after_seconds: int = RUN_HEARTBEAT_STALE_AFTER_SECONDS,
    ) -> dict[str, Any]:
        now = self._now()
        store_status = self._store_status()
        issues: list[dict[str, Any]] = []
        runtime: dict[str, Any] = {
            "store": store_status,
            "daemon_liveness": {},
            "last_run": None,
        }
        recent_failed_commands: list[dict[str, Any]] = []
        recent_failed_dispatch_actions: list[dict[str, Any]] = []

        if store_status["status"] != "available":
            issues.append(
                _health_issue(
                    issue_id=f"store-{store_status['status']}",
                    severity="critical",
                    category="store",
                    title=_store_issue_title(store_status["status"]),
                    detail=_store_issue_detail(store_status["status"]),
                    detected_at=now,
                    links=[{"type": "settings", "id": "storage"}],
                    recommended_actions=["inspect_settings", "run_doctor"],
                )
            )
            return _health_response(
                generated_at=now,
                runtime=runtime,
                issues=issues,
                recent_failed_commands=recent_failed_commands,
                recent_failed_dispatch_actions=recent_failed_dispatch_actions,
            )

        try:
            with self._connect() as conn:
                last_run = conn.execute(
                    # This selects a module-level constant column list.
                    f"SELECT {_RUN_RUNTIME_COLUMNS} FROM runs ORDER BY started_at DESC LIMIT 1"  # noqa: S608
                ).fetchone()
                daemon_run = conn.execute(
                    # This selects a module-level constant column list.
                    f"""
                    SELECT {_RUN_RUNTIME_COLUMNS}
                    FROM runs
                    WHERE last_tick_started_at IS NOT NULL
                    ORDER BY last_tick_started_at DESC, started_at DESC
                    LIMIT 1
                    """,  # noqa: S608
                ).fetchone()
                failed_commands = conn.execute(
                    """
                    SELECT message_id, command, status, result_json, updated_at
                    FROM approval_commands
                    WHERE status != 'applied' AND status != 'duplicate'
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (coerce_limit(limit),),
                ).fetchall()
                health_rows = conn.execute(
                    LATEST_NON_OK_HEALTH_CHECKS_SQL,
                    (coerce_limit(limit),),
                ).fetchall()
                failed_dispatch_rows = conn.execute(
                    """
                    SELECT a.*, t.short_id AS task_short_id
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE a.status IN ('failed', 'failed_needs_review')
                    ORDER BY a.updated_at DESC, a.id DESC
                    """
                ).fetchall()
                stale_rows = conn.execute(
                    """
                    SELECT a.*, t.short_id AS task_short_id
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE a.status = 'sending'
                      AND julianday(a.updated_at) <= julianday(?)
                    ORDER BY a.updated_at, a.id
                    """,
                    (_minus_seconds(now, stale_after_seconds),),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            issues.append(
                _health_issue(
                    issue_id="store-read-error",
                    severity="critical",
                    category="store",
                    title="Store read failed",
                    detail=_safe_detail(
                        str(exc), fallback="The operator store could not be read."
                    ),
                    detected_at=now,
                    links=[{"type": "settings", "id": "storage"}],
                    recommended_actions=["inspect_settings", "run_doctor"],
                )
            )
            return _health_response(
                generated_at=now,
                runtime=runtime,
                issues=issues,
                recent_failed_commands=recent_failed_commands,
                recent_failed_dispatch_actions=recent_failed_dispatch_actions,
            )

        try:
            last_run_dto = _run_runtime_summary(last_run) if last_run else None
            daemon_run_dto = _run_runtime_summary(daemon_run) if daemon_run else None
            daemon_liveness = _daemon_liveness(
                daemon_run_dto,
                now=now,
                stale_after_seconds=daemon_stale_after_seconds,
            )
            policy_status = self._policy_status()
            failed_dispatch_actions = [
                action_dto(row, include_payload=False) for row in failed_dispatch_rows
            ]
            stale_sending_actions = [
                action_dto(row, include_payload=False) for row in stale_rows
            ]
            recent_failed_commands = [
                _approval_command_summary(row) for row in failed_commands
            ]
            recent_failed_dispatch_actions = failed_dispatch_actions[
                : coerce_limit(limit)
            ]

            runtime |= {
                "daemon_liveness": daemon_liveness,
                "last_run": last_run_dto,
                "policy_status": policy_status,
            }

            daemon_issue = _daemon_health_issue(daemon_liveness, detected_at=now)
            if daemon_issue is not None:
                issues.append(daemon_issue)
            policy_issue = self.policy_health_issue(policy_status, detected_at=now)
            if policy_issue is not None:
                issues.append(policy_issue)
        except sqlite3.DatabaseError:
            runtime["store"] = {
                "status": "schema_incompatible",
                "available": False,
                "schema_initialized": False,
            }
            issues.append(
                _health_issue(
                    issue_id="store-schema_incompatible",
                    severity="critical",
                    category="store",
                    title=_store_issue_title("schema_incompatible"),
                    detail=_store_issue_detail("schema_incompatible"),
                    detected_at=now,
                    links=[{"type": "settings", "id": "storage"}],
                    recommended_actions=["inspect_settings", "run_doctor"],
                )
            )
            return _health_response(
                generated_at=now,
                runtime=runtime,
                issues=issues,
                recent_failed_commands=recent_failed_commands,
                recent_failed_dispatch_actions=recent_failed_dispatch_actions,
            )

        for row in health_rows:
            issue = _health_check_issue(row)
            if issue is not None:
                issues.append(issue)
        issues.extend(
            _dispatch_action_issue(action, detected_at=now)
            for action in failed_dispatch_actions
        )
        issues.extend(
            _stale_sending_issue(action, detected_at=now)
            for action in stale_sending_actions
        )

        issues.sort(
            key=lambda issue: (
                _SEVERITY_ORDER.get(issue["severity"], 0),
                str(issue["detected_at"]),
            ),
            reverse=True,
        )
        open_issue_count = len(issues)
        return _health_response(
            generated_at=now,
            runtime=runtime,
            issues=issues[: coerce_limit(limit)],
            open_issue_count=open_issue_count,
            recent_failed_commands=recent_failed_commands,
            recent_failed_dispatch_actions=recent_failed_dispatch_actions,
        )

    def _failed_approval_commands(self, *, limit: int) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT message_id, command, status, result_json, updated_at
                    FROM approval_commands
                    WHERE status != 'applied' AND status != 'duplicate'
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (coerce_limit(limit),),
                ).fetchall()
        except ReadStoreUnavailable:
            return []
        return [_approval_command_summary(row) for row in rows]

    def _recent_health_warnings(self, *, limit: int) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    LATEST_NON_OK_HEALTH_CHECKS_SQL,
                    (coerce_limit(limit),),
                ).fetchall()
        except ReadStoreUnavailable:
            return []
        return [_health_warning_dto(row) for row in rows]

    def _stale_sending_actions(
        self, *, stale_after_seconds: int, limit: int
    ) -> list[dict[str, Any]]:
        cutoff = _minus_seconds(self._now(), stale_after_seconds)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT a.*, t.short_id AS task_short_id
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE a.status = 'sending'
                      AND julianday(a.updated_at) <= julianday(?)
                    ORDER BY a.updated_at, a.id
                    LIMIT ?
                    """,
                    (cutoff, coerce_limit(limit)),
                ).fetchall()
        except ReadStoreUnavailable:
            return []
        return [action_dto(row, include_payload=False) for row in rows]

    def _store_status(self) -> dict[str, Any]:
        if not self.store.path.exists():
            return {
                "status": "missing",
                "available": False,
                "schema_initialized": False,
            }
        try:
            conn = sqlite3.connect(store_read_uri(self.store.path), uri=True)
        except sqlite3.OperationalError:
            return {
                "status": "unreadable",
                "available": False,
                "schema_initialized": False,
            }
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            schema_initialized = has_core_schema(conn)
        except sqlite3.DatabaseError:
            conn.close()
            return {
                "status": "schema_incompatible",
                "available": False,
                "schema_initialized": False,
            }
        conn.close()
        if not schema_initialized:
            return {
                "status": "schema_uninitialized",
                "available": False,
                "schema_initialized": False,
            }
        return {
            "status": "available",
            "available": True,
            "schema_initialized": True,
        }

    def policy_health_issue(
        self, policy_status: dict[str, Any], *, detected_at: str
    ) -> dict[str, Any] | None:
        return _policy_health_issue(
            policy_status,
            validate_policy_store=self._validate_policy_store,
            detected_at=detected_at,
        )

    def failed_approval_commands(self, *, limit: int) -> list[dict[str, Any]]:
        return self._failed_approval_commands(limit=limit)

    def recent_health_warnings(self, *, limit: int) -> list[dict[str, Any]]:
        return self._recent_health_warnings(limit=limit)

    def stale_sending_actions(
        self, *, stale_after_seconds: int, limit: int
    ) -> list[dict[str, Any]]:
        return self._stale_sending_actions(
            stale_after_seconds=stale_after_seconds, limit=limit
        )

    def store_status(self) -> dict[str, Any]:
        return self._store_status()


def _recent_errors(
    failed_commands: list[dict[str, Any]],
    failed_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for command in failed_commands:
        summary = _approval_command_summary(command)
        errors.append(
            {
                "type": "approval_command",
                "status": summary["status"],
                "message": summary["label"],
                "updated_at": summary["updated_at"],
            }
        )
    errors.extend(
        {
            "type": "dispatch_action",
            "status": action["status"],
            "message": f"{action['kind']} action {action['action_id']}",
            "updated_at": action["updated_at"],
        }
        for action in failed_actions
    )
    return sorted(errors, key=lambda item: str(item["updated_at"]), reverse=True)


def _health_response(
    *,
    generated_at: str,
    runtime: dict[str, Any],
    issues: list[dict[str, Any]],
    open_issue_count: int | None = None,
    recent_failed_commands: list[dict[str, Any]],
    recent_failed_dispatch_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    highest = "info"
    if issues:
        highest = max(
            issues, key=lambda issue: _SEVERITY_ORDER.get(issue["severity"], 0)
        )["severity"]
    return {
        "generated_at": generated_at,
        "summary": {
            "highest_severity": highest,
            "open_issue_count": len(issues)
            if open_issue_count is None
            else open_issue_count,
        },
        "runtime": runtime,
        "issues": issues,
        "recent_failed_commands": recent_failed_commands,
        "recent_failed_dispatch_actions": recent_failed_dispatch_actions,
    }


def _health_issue(
    *,
    issue_id: str,
    severity: str,
    category: str,
    title: str,
    detail: str,
    detected_at: str | None,
    links: list[dict[str, str]] | None = None,
    recommended_actions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "severity": severity,
        "category": category,
        "title": title,
        "detail": _safe_detail(detail, fallback=title),
        "detected_at": detected_at,
        "links": links or [],
        "recommended_actions": recommended_actions or [],
    }


def _daemon_health_issue(
    liveness: dict[str, Any], *, detected_at: str
) -> dict[str, Any] | None:
    status = str(liveness.get("status") or "unknown")
    if status == "live":
        return None
    if status == "not_started":
        return _health_issue(
            issue_id="daemon-not-started",
            severity="warning",
            category="daemon",
            title="Daemon has not started",
            detail="No daemon tick has been recorded in the operator store.",
            detected_at=detected_at,
            recommended_actions=["run_doctor", "start_daemon"],
        )
    if status == "stale":
        return _health_issue(
            issue_id="daemon-stale",
            severity="error",
            category="daemon",
            title="Daemon heartbeat is stale",
            detail="The last running daemon heartbeat is older than the configured liveness threshold.",
            detected_at=detected_at,
            recommended_actions=["inspect_runtime", "restart_daemon"],
        )
    if status == "stopped":
        return _health_issue(
            issue_id="daemon-stopped",
            severity="warning",
            category="daemon",
            title="Daemon is not running",
            detail="The latest daemon run is not currently marked running.",
            detected_at=detected_at,
            recommended_actions=["inspect_runtime", "start_daemon"],
        )
    return _health_issue(
        issue_id="daemon-unknown",
        severity="warning",
        category="daemon",
        title="Daemon liveness is unknown",
        detail="The operator store does not contain enough daemon heartbeat data.",
        detected_at=detected_at,
        recommended_actions=["inspect_runtime"],
    )


def _health_check_issue(row: sqlite3.Row) -> dict[str, Any] | None:
    data = row_dict(row)
    check_name = str(data.get("check_name") or "runtime")
    status = str(data.get("status") or "warning")
    severity = _health_check_severity(str(data.get("severity") or "warning"), status)
    detected_at = data.get("checked_at")
    return _health_issue(
        issue_id=f"health-{_slug(check_name)}-{_slug(str(detected_at or 'unknown'))}",
        severity=severity,
        category="runtime",
        title=f"{check_name} reported {status}",
        detail=str(
            data.get("message") or "Runtime health check reported a non-ok status."
        ),
        detected_at=detected_at,
        recommended_actions=["inspect_runtime"],
    )


def _health_warning_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = row_dict(row)
    data["message"] = _safe_detail(
        str(data.get("message") or ""),
        fallback="Runtime health check reported a non-ok status.",
    )
    return data


def _approval_command_summary(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        data = json_row_dict(row, "result_json")
    else:
        data = dict(row)
    if "label" in data and "result_summary" in data:
        return data
    verb, target_id = _parse_approval_command(str(data.get("command") or ""))
    raw_result = data.get("result_json")
    result = cast(dict[str, Any], raw_result) if isinstance(raw_result, dict) else {}
    summary: dict[str, Any] = {
        "message_id": data.get("message_id"),
        "verb": verb,
        "target_id": target_id,
        "status": data.get("status"),
        "updated_at": data.get("updated_at"),
        "label": _approval_command_label(verb, target_id),
        "result_summary": _approval_command_result_summary(result),
    }
    summary["command"] = summary["label"]
    return summary


def _parse_approval_command(command_text: str) -> tuple[str | None, str | None]:
    parts = command_text.split(maxsplit=2)
    if not parts:
        return None, None
    verb = parts[0].lstrip("/") or None
    target_id = parts[1] if len(parts) >= 2 else None
    return verb, target_id


def _approval_command_label(verb: str | None, target_id: str | None) -> str:
    if verb and target_id:
        return f"/{verb} {target_id}"
    if verb:
        return f"/{verb}"
    return "approval command"


def _approval_command_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    error = result.get("error")
    if error is not None:
        summary["error"] = _safe_detail(str(error), fallback="Approval command failed.")
    for key in ("action_id", "notification_action_id"):
        if key in result:
            summary[key] = result[key]
    pending_ids = result.get("pending_approval_ids")
    if isinstance(pending_ids, list):
        summary["pending_approval_count"] = len(cast(list[object], pending_ids))
    return summary


def _dispatch_action_issue(
    action: dict[str, Any], *, detected_at: str
) -> dict[str, Any]:
    status = str(action.get("status") or "")
    action_id = str(action.get("action_id"))
    is_needs_review = status == ActionStatus.FAILED_NEEDS_REVIEW.value
    title = (
        "Dispatch action needs review" if is_needs_review else "Dispatch action failed"
    )
    detail = (
        f"Action {action_id} failed after the actual-send boundary."
        if is_needs_review
        else f"Action {action_id} failed before it could be marked sent."
    )
    return _health_issue(
        issue_id=f"dispatch-action-{action_id}",
        severity="error",
        category="dispatch",
        title=title,
        detail=detail,
        detected_at=action.get("updated_at") or detected_at,
        links=[{"type": "dispatch_action", "id": action_id}],
        recommended_actions=_dispatch_action_names(status),
    )


def _stale_sending_issue(action: dict[str, Any], *, detected_at: str) -> dict[str, Any]:
    action_id = str(action.get("action_id"))
    return _health_issue(
        issue_id=f"dispatch-action-{action_id}-stale-sending",
        severity="warning",
        category="dispatch",
        title="Dispatch action is stale while sending",
        detail=f"Action {action_id} is still marked sending after the recovery threshold.",
        detected_at=action.get("updated_at") or detected_at,
        links=[{"type": "dispatch_action", "id": action_id}],
        recommended_actions=["inspect", "mark_sent", "cancel"],
    )


def _dispatch_action_names(status: str) -> list[str]:
    if status == ActionStatus.FAILED_NEEDS_REVIEW.value:
        return ["inspect", "mark_sent", "retry", "cancel"]
    if status == ActionStatus.FAILED.value:
        return ["inspect", "retry", "cancel"]
    return ["inspect"]


def _health_check_severity(severity: str, status: str) -> str:
    if severity == "critical":
        return "critical"
    if status == "failed":
        return "error"
    return "warning"


def _store_issue_title(status: str) -> str:
    return {
        "missing": "SQLite store is missing",
        "unreadable": "SQLite store is unreadable",
        "schema_uninitialized": "SQLite store schema is not initialized",
        "schema_incompatible": "SQLite store schema is incompatible",
    }.get(status, "SQLite store is unavailable")


def _store_issue_detail(status: str) -> str:
    return {
        "missing": "The operator store file does not exist yet, so console read models are unavailable.",
        "unreadable": "The operator store exists but cannot be opened by the local console.",
        "schema_uninitialized": "The operator store exists but required tables are missing.",
        "schema_incompatible": "The operator store could not be inspected with the expected schema contract.",
    }.get(status, "The operator store is unavailable.")


def _safe_detail(value: str, *, fallback: str) -> str:
    text = value.strip() or fallback
    return _ABSOLUTE_PATH_PATTERN.sub("[path]", text)


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug or "unknown"


def _store_read_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def _run_runtime_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "dry_run": bool(row["dry_run"]),
        "last_heartbeat_at": row["last_heartbeat_at"],
        "last_tick_started_at": row["last_tick_started_at"],
        "last_tick_finished_at": row["last_tick_finished_at"],
        "last_tick_status": row["last_tick_status"],
    }


def _daemon_liveness(
    last_run: dict[str, Any] | None,
    *,
    now: str,
    stale_after_seconds: int,
) -> dict[str, Any]:
    base: dict[str, Any] = {"stale_after_seconds": stale_after_seconds}
    if last_run is None:
        return base | {"status": "not_started", "stale": False}
    run_status = last_run.get("status")
    heartbeat = last_run.get("last_heartbeat_at")
    base |= {
        "run_id": last_run.get("run_id"),
        "run_status": run_status,
        "last_heartbeat_at": heartbeat,
    }
    if run_status != "running":
        return base | {"status": "stopped", "stale": False}
    heartbeat_dt = parse_datetime_or_none(heartbeat)
    now_dt = parse_datetime_or_none(now)
    if heartbeat_dt is None or now_dt is None:
        return base | {
            "status": "stale",
            "stale": True,
            "reason": "missing_or_invalid_heartbeat",
        }
    age_seconds = max(0, int((now_dt - heartbeat_dt).total_seconds()))
    stale = age_seconds > stale_after_seconds
    return base | {
        "status": "stale" if stale else "live",
        "stale": stale,
        "heartbeat_age_seconds": age_seconds,
    }


def _policy_health_issue(
    policy_status: dict[str, Any],
    *,
    validate_policy_store: Callable[[], None],
    detected_at: str,
) -> dict[str, Any] | None:
    if policy_status.get("initialized") is not True:
        return _health_issue(
            issue_id="policy-uninitialized",
            severity="critical",
            category="policy",
            title="Product Policy Store is not initialized",
            detail="Daemon runtime will fail closed until global Product Policy is imported.",
            detected_at=detected_at,
            links=[{"type": "policy", "id": "global"}],
            recommended_actions=["import_config"],
        )
    try:
        validate_policy_store()
    except ProductPolicyMissingError:
        return _health_issue(
            issue_id="policy-uninitialized",
            severity="critical",
            category="policy",
            title="Product Policy Store is not initialized",
            detail="Daemon runtime will fail closed until global Product Policy is imported.",
            detected_at=detected_at,
            links=[{"type": "policy", "id": "global"}],
            recommended_actions=["import_config"],
        )
    except ProductPolicyInvalidError as exc:
        return _health_issue(
            issue_id="policy-invalid",
            severity="critical",
            category="policy",
            title="Product Policy Store is invalid",
            detail=str(exc),
            detected_at=detected_at,
            links=[{"type": "policy", "id": "global"}],
            recommended_actions=["inspect_policy", "import_config"],
        )
    return None


def _minus_seconds(value: str, seconds: int) -> str:
    return shift_instant(value, delta=-timedelta(seconds=seconds))


# Public read-only helpers used by the operator DTO boundary.
RUN_RUNTIME_COLUMNS = _RUN_RUNTIME_COLUMNS
recent_errors = _recent_errors
run_runtime_summary = _run_runtime_summary
daemon_liveness = _daemon_liveness
store_read_uri = _store_read_uri
