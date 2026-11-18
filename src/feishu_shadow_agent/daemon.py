from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from .card_actions import FeishuCardActionConnection
from .config import AppConfig
from .coworker import CoworkerWorker
from .dispatcher import Dispatcher, DispatchSummary
from .feishu.client import FeishuClient
from .health import HealthSuite, has_critical_failure, summarize_results
from .ingestion import IngestionFeishuClient, IngestionService, StageResult
from .jsonl import JSONLLogger
from .membership import BotMembershipService
from .processing import ApprovalService, TaskProcessingService
from .retention import (
    RetentionService,
    daemon_retention_is_due,
    record_daemon_retention_checkpoint,
)
from .store.policy_store import COWORKER_POLICY_KEY
from .store.sqlite_store import SQLiteStore
from .types import BusinessMode, HealthCheckResult, RunTickStatus, new_run_id


class Daemon:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        logger: JSONLLogger,
        health_suite: HealthSuite,
        tick_interval_seconds: int,
        dry_run: bool,
        app_config: AppConfig | None = None,
        feishu_client: IngestionFeishuClient | None = None,
        task_processor: TaskProcessingService | None = None,
        run_metadata: dict[str, Any] | None = None,
        config_base_dir: str | Path | None = None,
        runtime_health_interval_seconds: int | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        ingestion_monotonic: Callable[[], float] | None = None,
        card_action_connection: FeishuCardActionConnection | None = None,
        coworker_worker: CoworkerWorker | None = None,
        business_mode: BusinessMode = "shadow",
        coworker_send_mode: str | None = None,
        coworker_chat_id: str | None = None,
    ):
        self.store = store
        self.logger = logger
        self.health_suite = health_suite
        self.tick_interval_seconds = tick_interval_seconds
        self.dry_run = dry_run
        self.app_config = app_config
        self.feishu_client = feishu_client
        self.task_processor = task_processor
        self.run_metadata = run_metadata or {}
        self.config_base_dir = (
            None if config_base_dir is None else Path(config_base_dir)
        )
        self.runtime_health_interval_seconds = runtime_health_interval_seconds
        self._health_lock = threading.RLock()
        self._last_runtime_health_at: float | None = None
        self._runtime_health_ok = False
        self.sleep_func = sleep_func
        self.ingestion_monotonic = ingestion_monotonic
        self._lifecycle_condition = threading.Condition()
        self._wakeup_generation = 0
        self._stop_requested = False
        self.card_action_connection = None if dry_run else card_action_connection
        self.coworker_worker = coworker_worker
        self.business_mode: BusinessMode = business_mode
        self.coworker_send_mode = coworker_send_mode
        self.coworker_chat_id = coworker_chat_id

    def wake(self) -> None:
        """Wake the daemon loop so queued operator actions dispatch promptly."""
        with self._lifecycle_condition:
            self._wakeup_generation += 1
            self._lifecycle_condition.notify_all()

    def request_stop(self) -> None:
        if self.coworker_worker is not None:
            self.coworker_worker.request_stop()
        with self._lifecycle_condition:
            self._stop_requested = True
            self._wakeup_generation += 1
            self._lifecycle_condition.notify_all()

    def run_startup_health(self) -> tuple[bool, list[HealthCheckResult]]:
        started_at = time.monotonic()
        results = self.health_suite.run(send_test=False)
        ok = not has_critical_failure(results)
        with self._health_lock:
            self._runtime_health_ok = ok
            self._last_runtime_health_at = started_at if ok else None
        return ok, results

    def runtime_health_allows_card_actions(self) -> bool:
        """Reject expired health state without triggering an extra probe."""
        return self._dispatch_health_cache_is_current()

    def run_one_noop_tick(self, *, run_id: str) -> None:
        self.logger.emit(
            "info",
            "daemon_tick_noop",
            run_id=run_id,
            data={"dry_run": self.dry_run},
        )

    def run_one_tick(self, *, run_id: str) -> list[StageResult]:
        heartbeat = HeartbeatRecorder(
            store=self.store, run_id=run_id, dry_run=self.dry_run
        )
        heartbeat.start()
        self.logger.debug(
            "daemon_tick_started",
            run_id=run_id,
            data={
                "dry_run": self.dry_run,
                "has_app_config": self.app_config is not None,
                "has_feishu_client": self.feishu_client is not None,
            },
        )
        results: list[StageResult] = []
        try:
            dispatch_enabled = (
                self.business_mode == "shadow" or self.coworker_send_mode == "project"
            )
            expired_approvals = (
                self.store.expire_pending_approvals(scope=self.business_mode)
                if dispatch_enabled
                else 0
            )
            if expired_approvals:
                self.logger.emit(
                    "info",
                    "approval_expiry_advanced",
                    run_id=run_id,
                    data={"expired_approvals": expired_approvals},
                )
            if self.app_config is None or self.feishu_client is None:
                self.run_one_noop_tick(run_id=run_id)
                result = StageResult("noop", ok=True)
                results.append(result)
                heartbeat.record(result)
                heartbeat.finish()
                return results
            policy_ok = self._runtime_product_policy_ok_for_tick(run_id=run_id)
            if not policy_ok:
                # Invalidate the cached verdict so callbacks are blocked now
                # and recovery requires a fresh critical health check.
                with self._health_lock:
                    self._runtime_health_ok = False
                    self._last_runtime_health_at = None
            if not policy_ok or not self._runtime_health_ok_for_tick(run_id=run_id):
                self.logger.emit(
                    "error",
                    "daemon_runtime_health_failed",
                    run_id=run_id,
                    data={"actual_sends_blocked": True},
                )
                result = StageResult(
                    "runtime_health", ok=False, error="runtime critical health failed"
                )
                results.append(result)
                heartbeat.record(result)
                heartbeat.finish()
                return results
            service = IngestionService(
                store=self.store,
                feishu_client=self.feishu_client,
                config=self.app_config,
                logger=self.logger,
                task_processor=self.task_processor,
                approval_service=(
                    ApprovalService(
                        store=self.store,
                        config=self.app_config,
                        execution_mode="dry_run" if self.dry_run else "production",
                        approval_scope="coworker",
                    )
                    if self.business_mode == "coworker" and dispatch_enabled
                    else None
                ),
                approval_scope=self.business_mode,
                config_base_dir=self.config_base_dir,
                monotonic=self.ingestion_monotonic,
                dry_run=self.dry_run,
            )
            membership = BotMembershipService(
                store=self.store,
                feishu_client=cast(Any, self.feishu_client),
                config=self.app_config,
                logger=self.logger,
                execution_mode="dry_run" if self.dry_run else "production",
                business_mode=self.business_mode,
            )

            def run_bot_membership(*, run_id: str) -> StageResult:
                summary = membership.refresh(
                    run_id=run_id,
                    chat_id=self.coworker_chat_id
                    if self.business_mode == "coworker"
                    else None,
                )
                return StageResult("bot_membership", ok=True, processed=summary.probed)

            approval_stage = (
                service.run_approval_inbox_placeholder
                if self.dry_run
                else service.run_approval_inbox
            )
            if self.business_mode == "shadow":
                stages = [
                    run_bot_membership,
                    service.run_processing_retries,
                    approval_stage,
                    *service.ordered_ingestion_stages(),
                ]
            elif dispatch_enabled:
                stages = [run_bot_membership, approval_stage]
            else:
                stages = []
            for stage in stages:
                try:
                    result = stage(run_id=run_id)
                except Exception as exc:  # noqa: BLE001
                    # Each daemon stage is an isolation boundary: record the
                    # failure and continue health/retention processing.
                    result = StageResult(_stage_name(stage), ok=False, error=str(exc))
                    if result.name == "approval_inbox":
                        self.store.record_health_results(
                            run_id=run_id,
                            results=[
                                HealthCheckResult(
                                    "approval_inbox",
                                    "warning",
                                    "failed",
                                    f"approval_inbox failed: {result.error}",
                                    {"stage": result.name, "error": result.error},
                                )
                            ],
                        )
                    self.logger.emit(
                        "error",
                        "daemon_stage_failed",
                        run_id=run_id,
                        data={"stage": result.name, "error": result.error},
                    )
                else:
                    if (
                        result.name == "approval_inbox"
                        and self.store.latest_health_check_status("approval_inbox")
                        not in (None, "ok")
                    ):
                        self.store.record_health_results(
                            run_id=run_id,
                            results=[
                                HealthCheckResult(
                                    "approval_inbox",
                                    "warning",
                                    "ok",
                                    "approval_inbox completed successfully",
                                    {
                                        "stage": result.name,
                                        "processed": result.processed,
                                    },
                                )
                            ],
                        )
                    self.logger.emit(
                        "info",
                        "daemon_stage_completed",
                        run_id=run_id,
                        data={
                            "stage": result.name,
                            "processed": result.processed,
                        },
                    )
                results.append(result)
                heartbeat.record(result)
            # A single agent call can outlast the health interval. Refresh only
            # when due before starting dispatch, while retaining the normal cache.
            if self._runtime_health_refresh_due(now=time.monotonic()) and not (
                self._runtime_health_ok_for_tick(run_id=run_id)
            ):
                self.logger.emit(
                    "error",
                    "daemon_runtime_health_failed",
                    run_id=run_id,
                    data={"actual_sends_blocked": True},
                )
                result = StageResult(
                    "runtime_health", ok=False, error="runtime critical health failed"
                )
                results.append(result)
                heartbeat.record(result)
                heartbeat.finish()
                return results
            approval_failed = any(
                result.name == "approval_inbox" and not result.ok for result in results
            )
            if approval_failed and not self.dry_run:
                self.logger.warning(
                    "daemon_dispatch_send_reply_blocked",
                    run_id=run_id,
                    data={
                        "reason": "approval_inbox_failed",
                        "owner_notifications_allowed": True,
                    },
                )
            dispatcher = Dispatcher(
                store=self.store,
                feishu_client=cast(FeishuClient, self.feishu_client),
                config=self.app_config,
                logger=self.logger,
                interactive_cards_available=(
                    self.app_config.interactive_cards.enabled
                    and not self.dry_run
                    and self.card_action_connection is not None
                    and self.card_action_connection.is_healthy()
                ),
            )
            # Owner commands are the safety valve for outgoing replies. If the inbox is
            # unhealthy, external send_reply actions stay preview-only while owner
            # notifications can still surface the failure.
            dispatch = (
                dispatcher.dispatch(
                    run_id=run_id,
                    allow_send_reply_actual=not self.dry_run and not approval_failed,
                    allow_owner_notification_actual=not self.dry_run,
                    include_owner_notifications=not self.dry_run,
                    blocked_send_reply_reason="approval_inbox_failed"
                    if approval_failed and not self.dry_run
                    else None,
                    can_start_action=self._dispatch_health_cache_is_current,
                    scope=self.business_mode,
                )
                if dispatch_enabled
                else DispatchSummary()
            )
            # The final action can itself consume the remaining cache lifetime.
            self._dispatch_health_cache_is_current()
            self.logger.emit(
                "info",
                "daemon_stage_completed",
                run_id=run_id,
                data={
                    "stage": "dispatch",
                    "processed": dispatch.processed,
                    "sent": dispatch.sent,
                    "previewed": dispatch.previewed,
                    "failed": dispatch.failed,
                    "approval_inbox_failed": approval_failed,
                    "owner_notifications_allowed": not self.dry_run,
                    "business_mode": self.business_mode,
                },
            )
            dispatch_error = (
                f"{dispatch.failed} dispatch action(s) failed"
                if dispatch.failed > 0
                else None
            )
            dispatch_result = StageResult(
                "dispatch",
                ok=dispatch.failed == 0,
                processed=dispatch.processed,
                error=dispatch_error,
            )
            results.append(dispatch_result)
            heartbeat.record(dispatch_result)
            retention_result = self._run_retention_stage(run_id=run_id)
            results.append(retention_result)
            heartbeat.record(retention_result)
            heartbeat.finish()
            return results
        except Exception as exc:
            result = StageResult("tick", ok=False, error=str(exc))
            results.append(result)
            heartbeat.record(result)
            heartbeat.finish()
            raise

    def run_forever(self) -> int:
        run_id = self.health_suite.run_id or new_run_id("run")
        self.store.record_run_start(
            run_id=run_id, dry_run=self.dry_run, **self.run_metadata
        )
        previous_signal_handlers = self._install_signal_handlers()
        finish_status: str | None = None
        try:
            self.logger.debug(
                "daemon_startup_health_started",
                run_id=run_id,
                data={"dry_run": self.dry_run, "run_metadata": self.run_metadata},
            )
            ok, results = self.run_startup_health()
            if not ok:
                summary = summarize_results(results)
                self.logger.emit(
                    "error", "daemon_startup_health_failed", run_id=run_id, data=summary
                )
                self.store.record_run_finish(
                    run_id=run_id, status="health_failed", health_summary=summary
                )
                return 2
            if self._stop_is_requested():
                finish_status = "stopped"
                return 0
            self._start_card_action_connection(run_id=run_id)
            if self._stop_is_requested():
                finish_status = "stopped"
                return 0
            self.store.set_checkpoint(
                "runtime.business_mode",
                {
                    "run_id": run_id,
                    "business_mode": self.business_mode,
                    "send_mode": self.coworker_send_mode,
                    "chat_id": self.coworker_chat_id,
                    "provider": self.coworker_worker.service.startup_provider
                    if self.business_mode == "coworker"
                    and self.coworker_worker is not None
                    else None,
                },
            )
            if self.coworker_worker is not None:
                self.coworker_worker.start()
            self.logger.emit(
                "info",
                "daemon_started",
                run_id=run_id,
                data={
                    "dry_run": self.dry_run,
                    "tick_interval_seconds": self.tick_interval_seconds,
                    "owner_notifications_allowed": not self.dry_run,
                    "business_mode": self.business_mode,
                    "coworker_send_mode": self.coworker_send_mode,
                },
            )
            observed_wakeup = self._current_wakeup_generation()
            while not self._stop_is_requested():
                self.run_one_tick(run_id=run_id)
                observed_wakeup = self._wait_for_next_tick(observed_wakeup)
        except KeyboardInterrupt:
            self.logger.emit("info", "daemon_interrupted", run_id=run_id)
            finish_status = "stopped"
            return 0
        except Exception as exc:
            self.logger.emit(
                "error",
                "daemon_crashed",
                run_id=run_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            finish_status = "crashed"
            raise
        else:
            finish_status = "stopped"
            return 0
        finally:
            try:
                if self.coworker_worker is not None:
                    self.coworker_worker.request_stop()
                if self.card_action_connection is not None:
                    self.card_action_connection.stop()
            finally:
                try:
                    if self.coworker_worker is not None:
                        self.coworker_worker.stop()
                finally:
                    self._restore_signal_handlers(previous_signal_handlers)
            if finish_status is not None:
                if finish_status == "stopped":
                    self.logger.emit("info", "daemon_stopped", run_id=run_id)
                self.store.record_run_finish(run_id=run_id, status=finish_status)

    def _start_card_action_connection(self, *, run_id: str) -> None:
        connection = self.card_action_connection
        if connection is None:
            return
        healthy = connection.start()
        snapshot = connection.snapshot()
        result = HealthCheckResult(
            "card_action_connection",
            "warning",
            "ok" if healthy else "failed",
            "interactive card callback connection is ready"
            if healthy
            else "interactive cards disabled at runtime; owner notifications use text fallback",
            snapshot,
        )
        self.store.record_health_results(run_id=run_id, results=[result])
        self.logger.emit(
            "info" if healthy else "warning",
            "card_action_connection_health",
            run_id=run_id,
            data=snapshot | {"text_fallback": not healthy},
        )

    def _current_wakeup_generation(self) -> int:
        with self._lifecycle_condition:
            return self._wakeup_generation

    def _stop_is_requested(self) -> bool:
        with self._lifecycle_condition:
            return self._stop_requested

    def _wait_for_next_tick(self, observed_wakeup: int) -> int:
        if self.sleep_func is not time.sleep:
            self.sleep_func(self.tick_interval_seconds)
            return self._current_wakeup_generation()
        with self._lifecycle_condition:
            self._lifecycle_condition.wait_for(
                lambda: (
                    self._stop_requested or self._wakeup_generation != observed_wakeup
                ),
                timeout=self.tick_interval_seconds,
            )
            return self._wakeup_generation

    def _install_signal_handlers(self) -> dict[int, Any]:
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous: dict[int, Any] = {}

        def request_controlled_stop(_signum: int, _frame: Any) -> None:
            self.request_stop()

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, request_controlled_stop)
        except ValueError:
            self._restore_signal_handlers(previous)
            return {}
        return previous

    @staticmethod
    def _restore_signal_handlers(previous: dict[int, Any]) -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    def _runtime_health_ok_for_tick(self, *, run_id: str) -> bool:
        now = time.monotonic()
        with self._health_lock:
            interval = self._runtime_health_check_interval()
            last_health_at = self._last_runtime_health_at
            cached_ok = self._runtime_health_ok
            reuse = last_health_at is not None and not self._runtime_health_refresh_due(
                now=now
            )
            if not reuse:
                # Card callbacks must not use the old verdict while a new
                # network health check is in progress.
                self._runtime_health_ok = False
                self._last_runtime_health_at = None
        if reuse and last_health_at is not None:
            self.logger.debug(
                "runtime_critical_health_reused",
                run_id=run_id,
                data={
                    "ok": cached_ok,
                    "interval_seconds": interval,
                    "age_seconds": round(now - last_health_at, 3),
                },
            )
            return cached_ok
        self.logger.debug(
            "runtime_critical_health_started",
            run_id=run_id,
            data={"interval_seconds": interval, "previous_ok": cached_ok},
        )
        if hasattr(self.health_suite, "run_runtime_critical"):
            results = self.health_suite.run_runtime_critical()
        else:  # pragma: no cover - compatibility for narrow tests
            results = self.health_suite.run(send_test=False)
        ok = not has_critical_failure(results)
        with self._health_lock:
            self._last_runtime_health_at = now
            self._runtime_health_ok = ok
        if not ok:
            self.logger.emit(
                "error",
                "runtime_critical_health_failed",
                run_id=run_id,
                data=summarize_results(results),
            )
        else:
            self.logger.debug(
                "runtime_critical_health_ok",
                run_id=run_id,
                data=summarize_results(results),
            )
        return ok

    def _runtime_health_refresh_due(self, *, now: float) -> bool:
        with self._health_lock:
            last_health_at = self._last_runtime_health_at
            return last_health_at is None or (
                now - last_health_at >= self._runtime_health_check_interval()
            )

    def _dispatch_health_cache_is_current(self) -> bool:
        # This only reads the local clock; a due refresh happens on the next tick.
        with self._health_lock:
            if not self._runtime_health_ok:
                return False
            if self._runtime_health_check_interval() <= 0:
                return True
            if not self._runtime_health_refresh_due(now=time.monotonic()):
                return True
            self._runtime_health_ok = False
            self._last_runtime_health_at = None
        self.wake()
        return False

    def _runtime_health_check_interval(self) -> int:
        with self._health_lock:
            if self.runtime_health_interval_seconds is not None:
                return self.runtime_health_interval_seconds
            if self.app_config is None:
                return 0
            health = self.app_config.health
            return (
                health.interval_seconds
                if self._runtime_health_ok
                else health.retry_interval_seconds
            )

    def _runtime_product_policy_ok_for_tick(self, *, run_id: str) -> bool:
        try:
            probe = self.store.product_policy_initialization_probe()
            raw_coworker = self.store.get_product_policy(COWORKER_POLICY_KEY)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - platform-specific detail
            # Store implementations can fail with platform-specific errors;
            # convert them into a health result for the current tick.
            result = HealthCheckResult(
                "product_policy_initialized",
                "critical",
                "failed",
                "Product Policy Store initialization check failed",
                {"error": str(exc), "path": str(self.store.path)},
            )
        else:
            if probe["initialized"]:
                configured_mode = (
                    "coworker"
                    if raw_coworker is not None and raw_coworker.get("enabled") is True
                    else "shadow"
                )
                configured_send_mode = (
                    raw_coworker.get("send_mode", "project")
                    if raw_coworker is not None
                    else None
                )
                if configured_mode != self.business_mode or (
                    configured_mode == "coworker"
                    and (
                        configured_send_mode != self.coworker_send_mode
                        or raw_coworker is None
                        or raw_coworker.get("chat_id") != self.coworker_chat_id
                    )
                ):
                    result = HealthCheckResult(
                        "business_mode",
                        "critical",
                        "failed",
                        "Business mode changed; restart the daemon to apply the new mode",
                        {
                            "running_mode": self.business_mode,
                            "configured_mode": configured_mode,
                        },
                    )
                    self.store.record_health_results(run_id=run_id, results=[result])
                    self.logger.emit(
                        "error", "daemon_business_mode_changed", run_id=run_id
                    )
                    return False
                return True
            result = HealthCheckResult(
                "product_policy_initialized",
                "critical",
                "failed",
                "Product Policy Store global policy is not initialized; run "
                + (
                    "`policy import-config --mode dry-run`."
                    if self.dry_run
                    else "`policy import-config`."
                ),
                {"missing": probe["missing"], "path": str(self.store.path)},
            )
        self.store.record_health_results(run_id=run_id, results=[result])
        self.logger.emit(
            "error",
            "runtime_critical_health_failed",
            run_id=run_id,
            data=summarize_results([result]),
        )
        return False

    def _run_retention_stage(self, *, run_id: str) -> StageResult:
        if self.app_config is None:
            return StageResult("retention", ok=True)
        if self.dry_run:
            self.logger.emit(
                "info",
                "retention_skipped",
                run_id=run_id,
                data={"reason": "dry_run"},
            )
            return StageResult("retention", ok=True)
        if not daemon_retention_is_due(self.store):
            self.logger.emit(
                "info",
                "retention_skipped",
                run_id=run_id,
                data={"reason": "not_due"},
            )
            return StageResult("retention", ok=True)
        base_dir = self.config_base_dir or Path.cwd()
        try:
            summary = RetentionService(
                store=self.store,
                config=self.app_config,
                base_dir=base_dir,
                logger=self.logger,
            ).prune(run_id=run_id, protect_pending=True)
            record_daemon_retention_checkpoint(self.store, summary=summary)
        except Exception as exc:  # noqa: BLE001
            # Retention is an independent daemon stage; surface failures while
            # preserving the daemon loop and its subsequent health checkpoint.
            self.logger.emit(
                "error",
                "daemon_stage_failed",
                run_id=run_id,
                data={"stage": "retention", "error": str(exc)},
            )
            return StageResult("retention", ok=False, error=str(exc))
        return StageResult(
            "retention",
            ok=True,
            processed=(
                sum(summary.content_scrubbed.values())
                + sum(summary.log_content_scrubbed.values())
                + summary.resources_expired
            ),
        )


class HeartbeatRecorder:
    def __init__(self, *, store: SQLiteStore, run_id: str, dry_run: bool):
        self.store = store
        self.run_id = run_id
        self.dry_run = dry_run
        self.results: list[StageResult] = []

    def start(self) -> None:
        self.store.record_run_tick_started(run_id=self.run_id, dry_run=self.dry_run)

    def record(self, result: StageResult) -> None:
        self.results.append(result)
        self.store.record_run_tick_progress(run_id=self.run_id, summary=self.summary())

    def finish(self) -> None:
        self.store.record_run_tick_finished(
            run_id=self.run_id,
            status=_run_tick_status(self.results),
            summary=self.summary(),
        )

    def summary(self) -> dict[str, Any]:
        failed = [result for result in self.results if not result.ok]
        return {
            "stages": [
                {
                    "name": result.name,
                    "ok": result.ok,
                    "processed": result.processed,
                    "error": result.error,
                }
                for result in self.results
            ],
            "processed": sum(result.processed for result in self.results),
            "failed": len(failed),
        }


def _stage_name(stage: Callable[..., StageResult]) -> str:
    name = getattr(stage, "__name__", "stage")
    if name == "run_approval_inbox":
        return "approval_inbox"
    if name == "ingest_group_at_me":
        return "group_at_me"
    if name == "ingest_p2p":
        return "p2p"
    if name == "run_active_watch":
        return "active_watch"
    return name


def _run_tick_status(results: list[StageResult]) -> str:
    if not results:
        return RunTickStatus.OK.value
    if all(result.ok for result in results):
        return RunTickStatus.OK.value
    if any(result.ok for result in results):
        return RunTickStatus.PARTIAL_FAILED.value
    return RunTickStatus.FAILED.value
