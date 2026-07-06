from __future__ import annotations

from pathlib import Path

from feishu_shadow_agent.config import AppConfig, LoadedConfig, OwnerConfig
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.maintenance_commands import MaintenanceCommandService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult


def test_doctor_command_result_distinguishes_dry_run_from_owner_send_test(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[bool] = []
    results = {
        False: [
            HealthCheckResult(
                name="owner_notification_dry_run",
                severity="warning",
                status="ok",
                message="ok",
            )
        ],
        True: [
            HealthCheckResult(
                name="owner_notification_send",
                severity="warning",
                status="ok",
                message="ok",
            )
        ],
    }
    service = _maintenance_service(
        tmp_path, monkeypatch, results_by_send_test=results, calls=calls
    )

    dry_run = service.doctor(send_test=False, actor="local_console")
    send_test = service.doctor(send_test=True, actor="local_console")

    assert calls == [False, True]
    assert dry_run.status == "no_change"
    assert dry_run.command == "maintenance.doctor"
    assert dry_run.changed is False
    assert dry_run.result["send_test"] is False
    assert send_test.status == "applied"
    assert send_test.command == "maintenance.doctor_send_test"
    assert send_test.changed is True
    assert send_test.result["send_test"] is True


def test_doctor_send_test_reports_failed_when_owner_send_fails(
    tmp_path: Path, monkeypatch
) -> None:
    service = _maintenance_service(
        tmp_path,
        monkeypatch,
        results_by_send_test={
            True: [
                HealthCheckResult(
                    name="owner_notification_send",
                    severity="warning",
                    status="failed",
                    message="send failed",
                )
            ]
        },
    )

    result = service.doctor(send_test=True, actor="local_console")

    assert result.status == "failed"
    assert result.command == "maintenance.doctor_send_test"
    assert result.changed is False
    assert result.warnings == ["owner_notification_send"]


def test_doctor_send_test_keeps_changed_when_owner_send_succeeds_before_critical_failure(
    tmp_path: Path, monkeypatch
) -> None:
    service = _maintenance_service(
        tmp_path,
        monkeypatch,
        results_by_send_test={
            True: [
                HealthCheckResult(
                    name="owner_notification_send",
                    severity="warning",
                    status="ok",
                    message="sent",
                ),
                HealthCheckResult(
                    name="hermes_reachable",
                    severity="critical",
                    status="failed",
                    message="down",
                ),
            ]
        },
    )

    result = service.doctor(send_test=True, actor="local_console")

    assert result.status == "failed"
    assert result.command == "maintenance.doctor_send_test"
    assert result.changed is True


def _maintenance_service(
    tmp_path: Path,
    monkeypatch,
    *,
    results_by_send_test: dict[bool, list[HealthCheckResult]],
    calls: list[bool] | None = None,
) -> MaintenanceCommandService:
    class FakeHealthSuite:
        def __init__(self, **_: object) -> None:
            pass

        def run(self, *, send_test: bool = False) -> list[HealthCheckResult]:
            if calls is not None:
                calls.append(send_test)
            return results_by_send_test[send_test]

    monkeypatch.setattr(
        "feishu_shadow_agent.maintenance_commands.HealthSuite", FakeHealthSuite
    )
    return MaintenanceCommandService(
        loaded_config=LoadedConfig(
            config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
            path=tmp_path / "config.yaml",
            base_dir=tmp_path,
            raw={"owner": {"open_id": "ou_owner"}},
        ),
        store=SQLiteStore(tmp_path / "agent.sqlite3"),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
    )
