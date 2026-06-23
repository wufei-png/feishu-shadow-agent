from __future__ import annotations

from pathlib import Path
import subprocess

from feishu_shadow_agent.config import ConfigService, LoadedConfig
from feishu_shadow_agent.health import HermesCliChecker, REQUIRED_USER_SCOPES, HealthSuite
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult, LarkCliResult

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.config.yaml"


class FakeFeishuClient:
    def __init__(self, *, scopes: set[str] | None = None, bot_available: bool = True):
        self.scopes = scopes if scopes is not None else set(REQUIRED_USER_SCOPES)
        self.bot_available = bot_available
        self.owner_message_dry_runs: list[bool] = []

    def version(self) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "--version"], 0, stdout="lark-cli version 1.0.56\n")

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return LarkCliResult(
            ["lark-cli", "auth", "status", "--json", "--verify"],
            0,
            json_data={
                "identities": {
                    "user": {"scope": " ".join(sorted(self.scopes))},
                    "bot": {
                        "available": self.bot_available,
                        "status": "ready" if self.bot_available else "missing",
                        "openId": "ou_bot" if self.bot_available else None,
                    },
                }
            },
        )

    def owner_message(
        self,
        *,
        owner_open_id: str,
        text: str,
        idempotency_key: str,
        dry_run: bool = True,
    ) -> LarkCliResult:
        self.owner_message_dry_runs.append(dry_run)
        return LarkCliResult(["lark-cli", "im", "+messages-send"], 0, json_data={"ok": True})


def ok_hermes(loaded: LoadedConfig) -> HealthCheckResult:
    return HealthCheckResult("hermes_reachable", "critical", "ok", "ok")


def failed_hermes(loaded: LoadedConfig) -> HealthCheckResult:
    return HealthCheckResult("hermes_reachable", "critical", "failed", "down")


def test_doctor_all_green_and_default_owner_notification_is_dry_run(tmp_path: Path) -> None:
    loaded = ConfigService().load(FIXTURE)
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    client = FakeFeishuClient()
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    assert not [result for result in results if result.is_critical_failure]
    assert client.owner_message_dry_runs == [True]


def test_lark_cli_version_records_resolved_path(tmp_path: Path) -> None:
    fake_cli = tmp_path / "lark-cli"
    fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    loaded = ConfigService().load(FIXTURE)
    loaded.config.lark_cli.path = str(fake_cli)
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    client = FakeFeishuClient()
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    version = next(result for result in results if result.name == "lark_cli_version")
    assert version.details["configured_path"] == str(fake_cli)
    assert version.details["resolved_path"] == str(fake_cli)


def test_send_test_owner_notification_is_not_dry_run(tmp_path: Path) -> None:
    loaded = ConfigService().load(FIXTURE)
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    client = FakeFeishuClient()
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    suite.run(send_test=True)

    assert client.owner_message_dry_runs == [False]


def test_missing_scope_is_critical_failure(tmp_path: Path) -> None:
    loaded = ConfigService().load(FIXTURE)
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    client = FakeFeishuClient(scopes=REQUIRED_USER_SCOPES - {"search:message"})
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    failed = {result.name: result for result in results if result.is_critical_failure}
    assert "required_user_scopes" in failed
    assert failed["required_user_scopes"].details["missing"] == ["search:message"]


def test_hermes_unreachable_is_critical_failure(tmp_path: Path) -> None:
    loaded = ConfigService().load(FIXTURE)
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    client = FakeFeishuClient()
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=failed_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    assert "hermes_reachable" in {result.name for result in results if result.is_critical_failure}


def test_hermes_cli_checker_version_critical_and_status_warning(
    monkeypatch,
) -> None:
    loaded = ConfigService().load(FIXTURE)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "--version":
            return subprocess.CompletedProcess(argv, 0, stdout="Hermes Agent v0.16.0\n", stderr="")
        if argv[1:3] == ["chat", "--help"]:
            return subprocess.CompletedProcess(argv, 0, stdout="usage: hermes chat [--toolsets TOOLSETS] [--yolo]\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = HermesCliChecker()(loaded)

    assert [result.name for result in results] == [
        "hermes_cli_version",
        "hermes_cli_status",
        "hermes_tool_permissions",
    ]
    assert results[0].status == "ok"
    assert results[1].severity == "warning"
    assert results[1].status == "failed"
    assert results[2].status == "ok"
    assert calls == [["hermes", "--version"], ["hermes", "status"], ["hermes", "chat", "--help"]]


def test_hermes_cli_checker_fails_when_full_access_yolo_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
tool_permissions: full_access
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)

    def fake_run(argv, **kwargs):
        if argv[1] == "--version":
            return subprocess.CompletedProcess(argv, 0, stdout="Hermes Agent v0.16.0\n", stderr="")
        if argv[1:3] == ["chat", "--help"]:
            return subprocess.CompletedProcess(argv, 0, stdout="usage: hermes chat [--toolsets TOOLSETS]\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = HermesCliChecker()(loaded)

    permissions = next(result for result in results if result.name == "hermes_tool_permissions")
    assert permissions.is_critical_failure
    assert permissions.details["missing_flags"] == ["--yolo"]
