from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import AppConfig
from .dispatcher import Dispatcher
from .feishu.client import FeishuClient
from .health import HealthSuite, has_critical_failure, summarize_results
from .ingestion import IngestionService, StageResult
from .jsonl import JSONLLogger
from .processing import TaskProcessingService
from .retention import (
    RetentionService,
    daemon_retention_is_due,
    record_daemon_retention_checkpoint,
)
from .store.sqlite_store import SQLiteStore
from .types import HealthCheckResult, RunTickStatus, new_run_id


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
        feishu_client: FeishuClient | None = None,
        task_processor: TaskProcessingService | None = None,
        send_owner_notifications: bool = False,
        run_metadata: dict[str, Any] | None = None,
        config_base_dir: str | Path | None = None,
        runtime_health_interval_seconds: int | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.logger = logger
        self.health_suite = health_suite
        self.tick_interval_seconds = tick_interval_seconds
        self.dry_run = dry_run
        self.app_config = app_config
        self.feishu_client = feishu_client
        self.task_processor = task_processor
        self.send_owner_notifications = send_owner_notifications
        self.run_metadata = run_metadata or {}
        self.config_base_dir = (
            None if config_base_dir is None else Path(config_base_dir)
        )
        self.runtime_health_interval_seconds = runtime_health_interval_seconds
        self._last_runtime_health_at: float | None = None
        self._runtime_health_ok = True
        self.sleep_func = sleep_func

    def run_startup_health(self) -> tuple[bool, list[HealthCheckResult]]:
        results = self.health_suite.run(send_test=False)
        return not has_critical_failure(results), results

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
                "send_owner_notifications": self.send_owner_notifications,
            },
        )
        results: list[StageResult] = []
        try:
            expired_approvals = self.store.expire_pending_approvals()
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
            if not self._runtime_product_policy_ok_for_tick(
                run_id=run_id
            ) or not self._runtime_health_ok_for_tick(run_id=run_id):
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
                config_base_dir=self.config_base_dir,
            )
            stages = [
                service.run_approval_inbox,
                service.ingest_group_at_me,
                service.ingest_p2p,
                service.run_active_watch,
            ]
            for stage in stages:
                try:
                    result = stage(run_id=run_id)
                except Exception as exc:
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
                            "send_owner_notifications": self.send_owner_notifications,
                        },
                    )
                results.append(result)
                heartbeat.record(result)
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
                feishu_client=self.feishu_client,
                config=self.app_config,
                logger=self.logger,
            )
            # Owner commands are the safety valve for outgoing replies. If the inbox is
            # unhealthy, external send_reply actions stay preview-only while owner
            # notifications can still surface the failure.
            dispatch = dispatcher.dispatch(
                run_id=run_id,
                allow_send_reply_actual=not self.dry_run and not approval_failed,
                allow_owner_notification_actual=not self.dry_run
                or self.send_owner_notifications,
                blocked_send_reply_reason="approval_inbox_failed"
                if approval_failed and not self.dry_run
                else None,
            )
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
                    "send_owner_notifications": self.send_owner_notifications,
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
            self.logger.emit(
                "info",
                "daemon_started",
                run_id=run_id,
                data={
                    "dry_run": self.dry_run,
                    "tick_interval_seconds": self.tick_interval_seconds,
                    "send_owner_notifications": self.send_owner_notifications,
                },
            )
            while True:
                self.run_one_tick(run_id=run_id)
                self.sleep_func(self.tick_interval_seconds)
        except KeyboardInterrupt:
            self.logger.emit("info", "daemon_interrupted", run_id=run_id)
            self.store.record_run_finish(run_id=run_id, status="interrupted")
            return 0

    def _runtime_health_ok_for_tick(self, *, run_id: str) -> bool:
        interval = self._runtime_health_check_interval()
        now = time.monotonic()
        if (
            self._last_runtime_health_at is not None
            and now - self._last_runtime_health_at < interval
        ):
            self.logger.debug(
                "runtime_critical_health_reused",
                run_id=run_id,
                data={
                    "ok": self._runtime_health_ok,
                    "interval_seconds": interval,
                    "age_seconds": round(now - self._last_runtime_health_at, 3),
                },
            )
            return self._runtime_health_ok
        self.logger.debug(
            "runtime_critical_health_started",
            run_id=run_id,
            data={"interval_seconds": interval, "previous_ok": self._runtime_health_ok},
        )
        if hasattr(self.health_suite, "run_runtime_critical"):
            results = self.health_suite.run_runtime_critical()
        else:  # pragma: no cover - compatibility for narrow tests
            results = self.health_suite.run(send_test=False)
        self._last_runtime_health_at = now
        self._runtime_health_ok = not has_critical_failure(results)
        if not self._runtime_health_ok:
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
        return self._runtime_health_ok

    def _runtime_health_check_interval(self) -> int:
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
        except Exception as exc:  # pragma: no cover - platform-specific detail
            result = HealthCheckResult(
                "product_policy_initialized",
                "critical",
                "failed",
                "Product Policy Store initialization check failed",
                {"error": str(exc), "path": str(self.store.path)},
            )
        else:
            if probe["initialized"]:
                return True
            result = HealthCheckResult(
                "product_policy_initialized",
                "critical",
                "failed",
                "Product Policy Store global policy is not initialized; run `policy import-config`.",
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
            ).prune(run_id=run_id)
            record_daemon_retention_checkpoint(self.store, summary=summary)
        except Exception as exc:
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
            processed=summary.raw_messages_pruned + summary.resources_expired,
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
