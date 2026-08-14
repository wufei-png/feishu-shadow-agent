from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .agent_backend import AgentRunResult
from .jsonl import JSONLLogger

AGENT_MAX_ATTEMPTS = 3
TERMINAL_AGENT_ERROR_MARKERS = (
    "no such file",
    "not found",
    "permission denied",
    "unknown option",
    "invalid option",
    "missing required",
    "configuration",
    "config",
    "auth",
    "permission",
)


@dataclass(frozen=True)
class AgentAttemptOutcome:
    result: AgentRunResult | None
    attempt_count: int
    last_error: str | None = None


class AgentInvoker:
    def __init__(
        self,
        *,
        logger: JSONLLogger,
        max_attempts: int = AGENT_MAX_ATTEMPTS,
        retry_delays_seconds: tuple[float, ...] = (1.0, 3.0),
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        self.logger = logger
        self.max_attempts = max(1, max_attempts)
        self.retry_delays_seconds = retry_delays_seconds
        self.sleep_func = sleep_func

    def call_with_retries(
        self,
        call: Callable[[], AgentRunResult],
        *,
        run_id: str | None,
        stage: str,
        message_id: str,
        task_id: int | None = None,
    ) -> AgentAttemptOutcome:
        last_result: AgentRunResult | None = None
        last_error: str | None = None
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            attempts = attempt
            self.logger.debug(
                "agent_call_attempt_started",
                run_id=run_id,
                task_id=None if task_id is None else str(task_id),
                data={"stage": stage, "message_id": message_id, "attempt": attempt},
            )
            try:
                result = call()
            except Exception as exc:  # noqa: BLE001
                # A backend callback is an extensibility boundary; retry and
                # report every backend failure without taking down ingestion.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_attempts:
                    self.logger.warning(
                        "agent_call_retrying",
                        run_id=run_id,
                        task_id=None if task_id is None else str(task_id),
                        data={
                            "stage": stage,
                            "message_id": message_id,
                            "attempt": attempt,
                            "error": last_error,
                        },
                    )
                    self._sleep_before_retry(attempt)
                else:
                    self.logger.error(
                        "agent_call_exception",
                        run_id=run_id,
                        task_id=None if task_id is None else str(task_id),
                        data={
                            "stage": stage,
                            "message_id": message_id,
                            "attempt": attempt,
                            "error": last_error,
                        },
                    )
                continue
            last_result = result
            if result.ok:
                self.logger.debug(
                    "agent_call_succeeded",
                    run_id=run_id,
                    task_id=None if task_id is None else str(task_id),
                    data={
                        "stage": stage,
                        "message_id": message_id,
                        "attempt": attempt,
                        "latency_ms": result.latency_ms,
                    },
                )
                return AgentAttemptOutcome(result=result, attempt_count=attempt)
            last_error = agent_result_error(result)
            if not is_retryable_agent_result(result):
                self.logger.error(
                    "agent_call_failed_terminal",
                    run_id=run_id,
                    task_id=None if task_id is None else str(task_id),
                    data={
                        "stage": stage,
                        "message_id": message_id,
                        "attempt": attempt,
                        "error": last_error,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                    },
                )
                return AgentAttemptOutcome(
                    result=result, attempt_count=attempt, last_error=last_error
                )
            if attempt < self.max_attempts:
                self.logger.warning(
                    "agent_call_retrying",
                    run_id=run_id,
                    task_id=None if task_id is None else str(task_id),
                    data={
                        "stage": stage,
                        "message_id": message_id,
                        "attempt": attempt,
                        "error": last_error,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                    },
                )
                self._sleep_before_retry(attempt)
            else:
                self.logger.error(
                    "agent_call_failed",
                    run_id=run_id,
                    task_id=None if task_id is None else str(task_id),
                    data={
                        "stage": stage,
                        "message_id": message_id,
                        "attempt": attempt,
                        "error": last_error,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                    },
                )
        return AgentAttemptOutcome(
            result=last_result, attempt_count=attempts, last_error=last_error
        )

    def _sleep_before_retry(self, attempt: int) -> None:
        if attempt <= 0 or not self.retry_delays_seconds:
            return
        index = min(attempt - 1, len(self.retry_delays_seconds) - 1)
        delay = self.retry_delays_seconds[index]
        if delay > 0:
            self.sleep_func(delay)


def agent_result_error(result: AgentRunResult) -> str:
    parts = [
        result.error,
        result.stderr,
        result.stdout,
        f"exit_code={result.exit_code}" if result.exit_code is not None else None,
        "timed_out=True" if result.timed_out else None,
    ]
    return (
        " ".join(str(part).strip() for part in parts if part).strip()
        or "Agent backend call failed"
    )


def is_retryable_agent_result(result: AgentRunResult) -> bool:
    error_text = agent_result_error(result).lower()
    if result.timed_out:
        return True
    if any(marker in error_text for marker in TERMINAL_AGENT_ERROR_MARKERS):
        return False
    if "stdout was not valid json" in error_text:
        return True
    if result.exit_code is None:
        return True
    return result.exit_code != 0


def truncate_error(value: str | None, *, limit: int = 1000) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."
