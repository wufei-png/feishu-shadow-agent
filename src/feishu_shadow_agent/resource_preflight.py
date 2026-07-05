from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .agent_invocation import truncate_error
from .jsonl import JSONLLogger
from .store.sqlite_store import SQLiteStore
from .types import NormalizedMessage, TaskRecord

RESOURCE_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class ResourcePreflightResult:
    allow: bool
    reason: str
    resources: list[Any]
    attempt_count: int = 0
    last_error: str | None = None


class ResourcePreflight:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        logger: JSONLLogger,
        max_attempts: int = RESOURCE_MAX_ATTEMPTS,
        retry_delays_seconds: tuple[float, ...] = (1.0, 3.0),
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.logger = logger
        self.max_attempts = max(1, max_attempts)
        self.retry_delays_seconds = retry_delays_seconds
        self.sleep_func = sleep_func
        self.retry_func: Callable[[NormalizedMessage, str | None], None] | None = None

    def set_retry_func(
        self, func: Callable[[NormalizedMessage, str | None], None]
    ) -> None:
        self.retry_func = func

    def check(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        prompt_message_ids: list[str],
        run_id: str,
    ) -> ResourcePreflightResult:
        resources = self.store.list_resources_for_messages(prompt_message_ids)
        state = resource_preflight_state(
            resources, message=message, prompt_message_ids=prompt_message_ids
        )
        if state["allow"]:
            return ResourcePreflightResult(True, "ok", resources)
        attempt_count = initial_resource_attempt_count(
            resources,
            message=message,
            prompt_message_ids=prompt_message_ids,
        )
        last_error = state["error"]
        if (
            state["retryable"]
            and self.retry_func is not None
            and has_current_prompt_resources(
                message=message,
                prompt_message_ids=prompt_message_ids,
            )
        ):
            for attempt in range(attempt_count + 1, self.max_attempts + 1):
                attempt_count = attempt
                self.logger.debug(
                    "resource_download_retry_started",
                    run_id=run_id,
                    task_id=str(task.id),
                    data={
                        "message_id": message.message_id,
                        "task_short_id": task.short_id,
                        "attempt": attempt,
                        "reason": state["reason"],
                    },
                )
                try:
                    retry_error = None
                    self.retry_func(message, run_id)
                except Exception as exc:
                    retry_error = f"{type(exc).__name__}: {exc}"
                    last_error = retry_error
                    self.logger.warning(
                        "resource_download_retry_exception",
                        run_id=run_id,
                        task_id=str(task.id),
                        data={
                            "message_id": message.message_id,
                            "task_short_id": task.short_id,
                            "attempt": attempt,
                            "error": truncate_error(retry_error),
                        },
                    )
                resources = self.store.list_resources_for_messages(prompt_message_ids)
                state = resource_preflight_state(
                    resources, message=message, prompt_message_ids=prompt_message_ids
                )
                if (
                    retry_error is None
                    and not state["allow"]
                    and state["error"] is not None
                ):
                    last_error = state["error"]
                if state["allow"]:
                    return ResourcePreflightResult(
                        True, "ok", resources, attempt_count=attempt_count
                    )
                if not state["retryable"]:
                    break
                if attempt < self.max_attempts:
                    self._sleep_before_retry(attempt)
        reason = state["reason"]
        if state["retryable"]:
            reason = "resource_download_failed"
            last_error = (
                last_error or state["error"] or resource_status_error(resources)
            )
        return ResourcePreflightResult(
            False,
            reason,
            resources,
            attempt_count=attempt_count,
            last_error=last_error or state["error"] or resource_status_error(resources),
        )

    def _sleep_before_retry(self, attempt: int) -> None:
        if attempt <= 0 or not self.retry_delays_seconds:
            return
        index = min(attempt - 1, len(self.retry_delays_seconds) - 1)
        delay = self.retry_delays_seconds[index]
        if delay > 0:
            self.sleep_func(delay)


def resource_preflight_state(
    resources: list[Any],
    *,
    message: NormalizedMessage,
    prompt_message_ids: list[str],
) -> dict[str, Any]:
    missing_current = missing_current_prompt_resources(
        resources,
        message=message,
        prompt_message_ids=prompt_message_ids,
    )
    if missing_current:
        return {
            "allow": False,
            "reason": "resource_missing",
            "retryable": True,
            "error": f"missing resource records: {', '.join(missing_current)}",
        }
    if not resources:
        return {"allow": True, "reason": "ok", "retryable": False, "error": None}
    statuses = {row["download_status"] for row in resources}
    if not statuses:
        return {"allow": True, "reason": "ok", "retryable": False, "error": None}
    if statuses <= {"downloaded"}:
        return {"allow": True, "reason": "ok", "retryable": False, "error": None}
    if statuses & {"bot_not_joined", "bot_invisible"}:
        return {
            "allow": False,
            "reason": "resource_needs_bot",
            "retryable": False,
            "error": resource_status_error(resources),
        }
    if statuses & {"skipped"}:
        return {
            "allow": False,
            "reason": "resource_download_disabled",
            "retryable": False,
            "error": resource_status_error(resources),
        }
    if statuses & {"too_large"}:
        return {
            "allow": False,
            "reason": "resource_too_large",
            "retryable": False,
            "error": resource_status_error(resources),
        }
    if statuses & {"quota_exceeded"}:
        return {
            "allow": False,
            "reason": "resource_quota_exceeded",
            "retryable": False,
            "error": resource_status_error(resources),
        }
    return {
        "allow": False,
        "reason": "resource_download_failed",
        "retryable": True,
        "error": resource_status_error(resources),
    }


def initial_resource_attempt_count(
    resources: list[Any],
    *,
    message: NormalizedMessage,
    prompt_message_ids: list[str],
) -> int:
    if not has_current_prompt_resources(
        message=message, prompt_message_ids=prompt_message_ids
    ):
        return 0
    current_keys = {
        (resource.message_id, resource.file_key, resource.resource_type)
        for resource in message.resources
    }
    row_keys = {
        (row["message_id"], row["file_key"], row["resource_type"]) for row in resources
    }
    return 1 if current_keys & row_keys else 0


def has_current_prompt_resources(
    *, message: NormalizedMessage, prompt_message_ids: list[str]
) -> bool:
    return message.message_id in set(prompt_message_ids) and bool(message.resources)


def missing_current_prompt_resources(
    resources: list[Any],
    *,
    message: NormalizedMessage,
    prompt_message_ids: list[str],
) -> list[str]:
    if not has_current_prompt_resources(
        message=message, prompt_message_ids=prompt_message_ids
    ):
        return []
    row_keys = {
        (row["message_id"], row["file_key"], row["resource_type"]) for row in resources
    }
    missing: list[str] = []
    for resource in message.resources:
        key = (resource.message_id, resource.file_key, resource.resource_type)
        if key not in row_keys:
            missing.append(f"{resource.resource_type}:{resource.file_key}")
    return missing


def resource_status_counts(resources: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in resources:
        status = str(row["download_status"])
        counts[status] = counts.get(status, 0) + 1
    return counts


def resource_status_error(resources: list[Any]) -> str | None:
    counts = resource_status_counts(resources)
    if not counts:
        return None
    return "resource statuses: " + ", ".join(
        f"{status}={count}" for status, count in sorted(counts.items())
    )
