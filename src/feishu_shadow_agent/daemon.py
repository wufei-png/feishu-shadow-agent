from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .config import AppConfig
from .feishu.client import FeishuClient
from .health import HealthSuite, has_critical_failure, summarize_results
from .ingestion import IngestionService, StageResult
from .jsonl import JSONLLogger
from .store.sqlite_store import SQLiteStore
from .types import HealthCheckResult, new_run_id


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
        send_owner_notifications: bool = False,
        run_metadata: dict[str, Any] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.logger = logger
        self.health_suite = health_suite
        self.tick_interval_seconds = tick_interval_seconds
        self.dry_run = dry_run
        self.app_config = app_config
        self.feishu_client = feishu_client
        self.send_owner_notifications = send_owner_notifications
        self.run_metadata = run_metadata or {}
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
        if self.app_config is None or self.feishu_client is None:
            self.run_one_noop_tick(run_id=run_id)
            return [StageResult("noop", ok=True)]
        service = IngestionService(
            store=self.store,
            feishu_client=self.feishu_client,
            config=self.app_config,
            logger=self.logger,
        )
        stages = [
            service.run_approval_inbox_placeholder,
            service.ingest_group_at_me,
            service.ingest_p2p,
            service.run_active_watch,
            self._dispatch_placeholder,
        ]
        results: list[StageResult] = []
        for stage in stages:
            try:
                result = stage(run_id=run_id)
            except Exception as exc:
                result = StageResult(getattr(stage, "__name__", "stage"), ok=False, error=str(exc))
                self.logger.emit(
                    "error",
                    "daemon_stage_failed",
                    run_id=run_id,
                    data={"stage": result.name, "error": result.error},
                )
            else:
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
        return results

    def _dispatch_placeholder(self, *, run_id: str) -> StageResult:
        pending_actions = self.store.count_pending_actions()
        self.logger.emit(
            "info",
            "dispatch_placeholder",
            run_id=run_id,
            data={"pending_actions": pending_actions, "dry_run": self.dry_run},
        )
        return StageResult("dispatch", ok=True, processed=pending_actions)

    def run_forever(self) -> int:
        run_id = self.health_suite.run_id or new_run_id("run")
        self.store.record_run_start(run_id=run_id, dry_run=self.dry_run, **self.run_metadata)
        try:
            ok, results = self.run_startup_health()
            if not ok:
                summary = summarize_results(results)
                self.logger.emit("error", "daemon_startup_health_failed", run_id=run_id, data=summary)
                self.store.record_run_finish(run_id=run_id, status="health_failed", health_summary=summary)
                return 2
            self.logger.emit("info", "daemon_started", run_id=run_id, data={"dry_run": self.dry_run})
            while True:
                self.run_one_tick(run_id=run_id)
                self.sleep_func(self.tick_interval_seconds)
        except KeyboardInterrupt:
            self.logger.emit("info", "daemon_interrupted", run_id=run_id)
            self.store.record_run_finish(run_id=run_id, status="interrupted")
            return 0
