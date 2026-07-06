from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .agent_invocation import AgentInvoker
from .config import ConfigError, ConfigService, LoadedConfig
from .feishu.lark_cli import LarkCliClient
from .health import HealthSuite, has_critical_failure, summarize_results
from .hermes import HermesCliClient
from .jsonl import JSONLLogger
from .operator_commands import CommandResult
from .reply_style import ReplyStyleRefresher
from .retention import RetentionService
from .store.sqlite_store import SQLiteStore
from .types import HealthCheckResult, new_run_id


class MaintenanceCommandService:
    def __init__(
        self,
        *,
        loaded_config: LoadedConfig,
        store: SQLiteStore,
        logger: JSONLLogger,
    ):
        self.loaded_config = loaded_config
        self.store = store
        self.logger = logger

    def doctor(
        self,
        *,
        send_test: bool,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        run_id = new_run_id("doctor")
        self.store.record_run_start(run_id=run_id, dry_run=not send_test)
        suite = HealthSuite(
            loaded_config=self.loaded_config,
            store=self.store,
            feishu_client=self._feishu_client(),
            run_id=run_id,
        )
        results = suite.run(send_test=send_test)
        summary = summarize_results(results)
        failed = has_critical_failure(results)
        self.store.record_run_finish(
            run_id=run_id,
            status="health_failed" if failed else "ok",
            health_summary=summary,
        )
        status, changed = _doctor_command_outcome(
            send_test=send_test, results=results, has_critical_failure=failed
        )
        self.logger.emit("info", "doctor_completed", run_id=run_id, data=summary)
        return CommandResult(
            status=status,
            command="maintenance.doctor_send_test"
            if send_test
            else "maintenance.doctor",
            actor=actor,
            reason=reason,
            target={"type": "runtime_health"},
            changed=changed,
            result={
                "run_id": run_id,
                "send_test": send_test,
                "summary": summary,
                "results": [_health_result_dict(result) for result in results],
            },
            warnings=[
                result.name
                for result in results
                if result.severity == "warning" and result.status != "ok"
            ],
            next_actions=[{"command": "health", "target": {"type": "console_route"}}],
        )

    def config_validate(
        self,
        *,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        try:
            loaded = ConfigService().load(self.loaded_config.path)
        except ConfigError as exc:
            return CommandResult(
                status="validation_failed",
                command="maintenance.config_validate",
                actor=actor,
                reason=reason,
                target={"type": "config", "path": str(self.loaded_config.path)},
                changed=False,
                result={"error": str(exc)},
            )
        return CommandResult(
            status="no_change",
            command="maintenance.config_validate",
            actor=actor,
            reason=reason,
            target={"type": "config", "path": str(loaded.path)},
            changed=False,
            result={"path": str(loaded.path), "base_dir": str(loaded.base_dir)},
        )

    def retention_prune(
        self,
        *,
        dry_run: bool,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        run_id = new_run_id("retention")
        summary = RetentionService(
            store=self.store,
            config=self.loaded_config.config,
            base_dir=self.loaded_config.base_dir,
            logger=self.logger,
        ).prune(run_id=run_id, dry_run=dry_run)
        changed = not dry_run and (
            summary.raw_messages_pruned > 0
            or summary.resources_deleted > 0
            or summary.resources_expired > 0
        )
        return CommandResult(
            status="no_change" if dry_run or not changed else "applied",
            command="maintenance.retention_prune",
            actor=actor,
            reason=reason,
            target={"type": "retention"},
            changed=changed,
            result={"run_id": run_id, **summary.as_dict()},
        )

    def reply_style_refresh(
        self,
        *,
        dry_run: bool,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        run_id = new_run_id("reply_style")
        result = ReplyStyleRefresher(
            config=self.loaded_config.config,
            base_dir=self.loaded_config.base_dir,
            feishu_client=self._feishu_client(),
            agent_backend=self._agent_backend(),
            agent_invoker=AgentInvoker(logger=self.logger),
        ).refresh(dry_run=dry_run, run_id=run_id)
        changed = result.wrote_profile
        return CommandResult(
            status="applied"
            if result.status == "written"
            else "no_change"
            if result.status == "dry_run"
            else "failed",
            command="maintenance.reply_style_refresh",
            actor=actor,
            reason=reason,
            target={"type": "reply_style_profile"},
            changed=changed,
            result={"run_id": run_id, **result.as_dict()},
        )

    def _feishu_client(self) -> LarkCliClient:
        return LarkCliClient(
            path=self.loaded_config.config.lark_cli.path,
            timeout_seconds=self.loaded_config.config.lark_cli.timeout_seconds,
            cwd=self.loaded_config.base_dir,
        )

    def _agent_backend(self) -> HermesCliClient:
        backend_config = self.loaded_config.config.agent_backend
        return HermesCliClient(
            config=backend_config.hermes,
            tool_permissions=self.loaded_config.config.tool_permissions,
            config_scope=backend_config.config_scope,
            auto_context=backend_config.auto_context,
            reply_postprocess=self.loaded_config.config.reply_postprocess,
        )


def _health_result_dict(result: HealthCheckResult) -> dict[str, Any]:
    return asdict(result)


def _doctor_command_outcome(
    *,
    send_test: bool,
    results: list[HealthCheckResult],
    has_critical_failure: bool,
) -> tuple[str, bool]:
    if not send_test:
        return ("failed", False) if has_critical_failure else ("no_change", False)
    owner_send_ok = any(
        result.name == "owner_notification_send" and result.status == "ok"
        for result in results
    )
    if has_critical_failure or not owner_send_ok:
        return "failed", owner_send_ok
    return "applied", True
