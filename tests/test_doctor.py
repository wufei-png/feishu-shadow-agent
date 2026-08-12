from __future__ import annotations

import subprocess
from pathlib import Path

from feishu_shadow_agent.config import ConfigService, LoadedConfig
from feishu_shadow_agent.health import (
    REQUIRED_USER_SCOPES,
    ClaudeCodeCliChecker,
    CodexCliChecker,
    HealthSuite,
    HermesCliChecker,
    HermesHealthChecker,
    SelectedBackendReadinessChecker,
)
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult, LarkCliResult

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.config.yaml"


class FakeFeishuClient:
    def __init__(self, *, scopes: set[str] | None = None, bot_available: bool = True):
        self.scopes = scopes if scopes is not None else set(REQUIRED_USER_SCOPES)
        self.bot_available = bot_available
        self.owner_message_dry_runs: list[bool] = []

    def version(self) -> LarkCliResult:
        return LarkCliResult(
            ["lark-cli", "--version"], 0, stdout="lark-cli version 1.0.56\n"
        )

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
        return LarkCliResult(
            ["lark-cli", "im", "+messages-send"], 0, json_data={"ok": True}
        )


def ok_hermes(loaded: LoadedConfig) -> HealthCheckResult:
    return HealthCheckResult("hermes_reachable", "critical", "ok", "ok")


def failed_hermes(loaded: LoadedConfig) -> HealthCheckResult:
    return HealthCheckResult("hermes_reachable", "critical", "failed", "down")


def _initialized_store(tmp_path: Path, loaded: LoadedConfig) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.import_product_policy_from_config(
        loaded.config,
        used_defaults=loaded.reply_policy_used_defaults,
    )
    return store


def test_http_hermes_health_mode_still_checks_cli_runtime_backend(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  hermes:
    mode: http
    health_url: http://127.0.0.1:8642/health
    api_key_env: null
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    calls: list[str] = []

    def cli_checker(loaded: LoadedConfig) -> list[HealthCheckResult]:
        calls.append("cli")
        return [
            HealthCheckResult("hermes_cli_version", "critical", "failed", "missing")
        ]

    def http_checker(loaded: LoadedConfig) -> HealthCheckResult:
        calls.append("http")
        return HealthCheckResult("hermes_reachable", "critical", "ok", "ok")

    results = HermesHealthChecker(cli_checker=cli_checker, http_checker=http_checker)(
        loaded
    )

    assert calls == ["cli", "http"]
    assert "hermes_cli_version" in {
        result.name for result in results if result.is_critical_failure
    }


def test_selected_backend_readiness_checker_delegates_to_configured_provider() -> None:
    loaded = ConfigService().load(FIXTURE)
    calls: list[str] = []

    def hermes_checker(loaded: LoadedConfig) -> HealthCheckResult:
        calls.append(loaded.config.agent_backend.provider)
        return HealthCheckResult("hermes_cli_version", "critical", "ok", "ok")

    results = SelectedBackendReadinessChecker(hermes_checker=hermes_checker)(loaded)

    assert calls == ["hermes"]
    assert [result.name for result in results] == ["hermes_cli_version"]


def test_selected_backend_readiness_checker_delegates_to_codex_provider(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: codex
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    calls: list[str] = []

    def codex_checker(loaded: LoadedConfig) -> HealthCheckResult:
        calls.append(loaded.config.agent_backend.provider)
        return HealthCheckResult("codex_cli_version", "critical", "ok", "ok")

    results = SelectedBackendReadinessChecker(codex_checker=codex_checker)(loaded)

    assert calls == ["codex"]
    assert [result.name for result in results] == ["codex_cli_version"]


def test_selected_backend_readiness_checker_delegates_to_claude_code_provider(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: claude_code
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    calls: list[str] = []

    def claude_code_checker(loaded: LoadedConfig) -> HealthCheckResult:
        calls.append(loaded.config.agent_backend.provider)
        return HealthCheckResult("claude_code_cli_version", "critical", "ok", "ok")

    results = SelectedBackendReadinessChecker(claude_code_checker=claude_code_checker)(
        loaded
    )

    assert calls == ["claude_code"]
    assert [result.name for result in results] == ["claude_code_cli_version"]


def test_doctor_all_green_and_default_owner_notification_is_dry_run(
    tmp_path: Path,
) -> None:
    loaded = ConfigService().load(FIXTURE)
    store = _initialized_store(tmp_path, loaded)
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


def test_doctor_fails_when_product_policy_store_is_not_initialized(
    tmp_path: Path,
) -> None:
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

    failed = {result.name: result for result in results if result.is_critical_failure}
    assert failed["product_policy_initialized"].details["missing"] == [
        "global:reply_policy"
    ]
    assert "policy import-config" in failed["product_policy_initialized"].message


def test_lark_cli_version_records_resolved_path(tmp_path: Path) -> None:
    fake_cli = tmp_path / "lark-cli"
    fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    loaded = ConfigService().load(FIXTURE)
    loaded.config.lark_cli.path = str(fake_cli)
    store = _initialized_store(tmp_path, loaded)
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
    store = _initialized_store(tmp_path, loaded)
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


def test_doctor_warns_when_enabled_reply_postprocess_owner_profile_is_missing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
reply_postprocess:
  enabled: true
  owner_style:
    enabled: true
    profile_path: missing-owner-style.md
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    store = _initialized_store(tmp_path, loaded)
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=FakeFeishuClient(),
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    warning = next(
        result
        for result in results
        if result.name == "reply_postprocess_owner_style_profile"
    )
    assert warning.severity == "warning"
    assert warning.status == "failed"
    assert not [result for result in results if result.is_critical_failure]


def test_doctor_warns_when_enabled_reply_postprocess_humanizer_skill_is_missing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
reply_postprocess:
  enabled: true
  humanizer_zh:
    enabled: true
    skill_path: missing-humanizer.md
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    store = _initialized_store(tmp_path, loaded)
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=FakeFeishuClient(),
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    warning = next(
        result
        for result in results
        if result.name == "reply_postprocess_humanizer_zh_skill"
    )
    assert warning.severity == "warning"
    assert warning.status == "failed"
    assert not [result for result in results if result.is_critical_failure]


def test_missing_scope_is_critical_failure(tmp_path: Path) -> None:
    loaded = ConfigService().load(FIXTURE)
    store = _initialized_store(tmp_path, loaded)
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
    store = _initialized_store(tmp_path, loaded)
    client = FakeFeishuClient()
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=failed_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    assert "hermes_reachable" in {
        result.name for result in results if result.is_critical_failure
    }


def test_runtime_critical_health_includes_agent_backend(tmp_path: Path) -> None:
    loaded = ConfigService().load(FIXTURE)
    store = _initialized_store(tmp_path, loaded)
    client = FakeFeishuClient()
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=failed_hermes,
        run_id="doctor_1",
    )

    results = suite.run_runtime_critical()

    assert "hermes_reachable" in {
        result.name for result in results if result.is_critical_failure
    }
    assert client.owner_message_dry_runs == []


def test_hermes_cli_checker_version_critical_and_status_warning(
    monkeypatch,
) -> None:
    loaded = ConfigService().load(FIXTURE)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="Hermes Agent v0.16.0\n", stderr=""
            )
        if argv[1:3] == ["chat", "--help"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="usage: hermes chat [--toolsets TOOLSETS] [--yolo] [--ignore-user-config] [--ignore-rules]\n",
                stderr="",
            )
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
    assert calls == [
        ["hermes", "--version"],
        ["hermes", "status"],
        ["hermes", "chat", "--help"],
    ]


def test_backend_health_probe_keeps_finite_health_timeout_when_agent_is_unbounded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
health:
  timeout_seconds: 7
agent_backend:
  hermes:
    timeout_seconds: null
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    timeouts: list[int] = []

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        stdout = (
            "usage: hermes chat [--toolsets TOOLSETS] [--ignore-user-config] "
            "[--ignore-rules]\n"
            if argv[1:3] == ["chat", "--help"]
            else "ok\n"
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    HermesCliChecker()(loaded)

    assert loaded.config.agent_backend.hermes.timeout_seconds is None
    assert timeouts == [7, 7, 7]


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
            return subprocess.CompletedProcess(
                argv, 0, stdout="Hermes Agent v0.16.0\n", stderr=""
            )
        if argv[1:3] == ["chat", "--help"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="usage: hermes chat [--toolsets TOOLSETS] [--ignore-user-config] [--ignore-rules]\n",
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = HermesCliChecker()(loaded)

    permissions = next(
        result for result in results if result.name == "hermes_tool_permissions"
    )
    assert permissions.is_critical_failure
    assert permissions.details["missing_flags"] == ["--yolo"]


def test_hermes_cli_checker_fails_when_default_context_flags_are_missing(
    monkeypatch,
) -> None:
    loaded = ConfigService().load(FIXTURE)

    def fake_run(argv, **kwargs):
        if argv[1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="Hermes Agent v0.15.0\n", stderr=""
            )
        if argv[1:3] == ["chat", "--help"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="usage: hermes chat [--toolsets TOOLSETS]\n", stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = HermesCliChecker()(loaded)

    permissions = next(
        result for result in results if result.name == "hermes_tool_permissions"
    )
    assert permissions.is_critical_failure
    assert permissions.details["missing_flags"] == [
        "--ignore-user-config",
        "--ignore-rules",
    ]


def test_hermes_cli_checker_requires_skills_flag_only_when_skills_are_configured(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  hermes:
    skill_paths:
      - skills/support
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)

    def fake_run(argv, **kwargs):
        if argv[1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="Hermes Agent v0.16.0\n", stderr=""
            )
        if argv[1:3] == ["chat", "--help"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="usage: hermes chat [--toolsets TOOLSETS] [--ignore-user-config] [--ignore-rules]\n",
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = HermesCliChecker()(loaded)

    permissions = next(
        result for result in results if result.name == "hermes_tool_permissions"
    )
    assert permissions.is_critical_failure
    assert permissions.details["missing_flags"] == ["--skills"]


def test_hermes_cli_checker_requires_configured_model_provider_flags(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  hermes:
    model: test-model
    provider: test-provider
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)

    def fake_run(argv, **kwargs):
        if argv[1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="Hermes Agent v0.16.0\n", stderr=""
            )
        if argv[1:3] == ["chat", "--help"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="usage: hermes chat [--toolsets TOOLSETS] [--ignore-user-config] [--ignore-rules]\n",
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = HermesCliChecker()(loaded)

    permissions = next(
        result for result in results if result.name == "hermes_tool_permissions"
    )
    assert permissions.is_critical_failure
    assert permissions.details["missing_flags"] == ["--model", "--provider"]


def test_codex_cli_checker_checks_version_login_and_exec_capabilities(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: codex
  codex:
    path: /bin/codex
    model: gpt-5
tool_permissions: read_only
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="codex-cli 0.142.5\n", stderr=""
            )
        if argv[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="Logged in using ChatGPT\n", stderr=""
            )
        if "resume" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="Usage: codex exec resume [--output-schema <FILE>] [--output-last-message <FILE>] [--json]\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Usage: codex exec [--sandbox <MODE>] [--ignore-user-config] [--ignore-rules] [--output-schema <FILE>] [--output-last-message <FILE>] [--json] [--skip-git-repo-check] [--model <MODEL>] resume\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = CodexCliChecker()(loaded)

    assert [result.name for result in results] == [
        "codex_cli_version",
        "codex_login_status",
        "codex_exec_capabilities",
    ]
    assert [result.status for result in results] == ["ok", "ok", "ok"]
    assert calls == [
        ["/bin/codex", "--version"],
        ["/bin/codex", "login", "status"],
        [
            "/bin/codex",
            "--search",
            "--ask-for-approval",
            "never",
            "exec",
            "--help",
        ],
        [
            "/bin/codex",
            "--search",
            "--ask-for-approval",
            "never",
            "exec",
            "--sandbox",
            "read-only",
            "resume",
            "--help",
        ],
    ]


def test_codex_cli_checker_matches_context_mode_specific_exec_flags(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def make_fake_run(exec_help: str):
        def fake_run(argv, **kwargs):
            if argv[1] == "--version":
                return subprocess.CompletedProcess(
                    argv, 0, stdout="codex-cli 0.142.5\n", stderr=""
                )
            if argv[1:3] == ["login", "status"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="Logged in using ChatGPT\n", stderr=""
                )
            if "resume" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout="Usage: codex exec resume [--output-schema <FILE>] [--output-last-message <FILE>] [--json]\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout=exec_help, stderr="")

        return fake_run

    cases = [
        (
            "native",
            "  config_scope: native\n",
            "Usage: codex exec [--sandbox <MODE>] [--ignore-rules] [--output-schema <FILE>] [--output-last-message <FILE>] [--json] [--skip-git-repo-check] resume\n",
        ),
        (
            "auto-context-enabled",
            "  auto_context: enabled\n",
            "Usage: codex exec [--sandbox <MODE>] [--ignore-user-config] [--output-schema <FILE>] [--output-last-message <FILE>] [--json] [--skip-git-repo-check] resume\n",
        ),
    ]
    for name, backend_context, exec_help in cases:
        config_path = tmp_path / f"{name}.yaml"
        config_path.write_text(
            f"""
owner:
  open_id: ou_owner
agent_backend:
  provider: codex
{backend_context}  codex:
    path: /bin/codex
tool_permissions: read_only
""",
            encoding="utf-8",
        )
        loaded = ConfigService().load(config_path)

        monkeypatch.setattr(subprocess, "run", make_fake_run(exec_help))

        results = CodexCliChecker()(loaded)

        assert [result.status for result in results] == ["ok", "ok", "ok"]


def test_codex_cli_checker_requires_full_access_bypass_flag(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: codex
tool_permissions: full_access
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)

    def fake_run(argv, **kwargs):
        if argv[1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="codex-cli 0.142.5\n", stderr=""
            )
        if argv[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="Logged in using ChatGPT\n", stderr=""
            )
        if "resume" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="Usage: codex exec resume [--output-schema <FILE>] [--output-last-message <FILE>] [--json]\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Usage: codex exec [--sandbox <MODE>] [--ignore-user-config] [--ignore-rules] [--output-schema <FILE>] [--output-last-message <FILE>] [--json] [--skip-git-repo-check]\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = CodexCliChecker()(loaded)

    capabilities = next(
        result for result in results if result.name == "codex_exec_capabilities"
    )
    assert capabilities.is_critical_failure
    assert capabilities.details["missing_flags"] == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]


def test_claude_code_cli_checker_checks_auth_and_print_capabilities(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: claude_code
  claude_code:
    path: /bin/claude
    model: sonnet
tool_permissions: read_only
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="2.1.201 (Claude Code)\n", stderr=""
            )
        if argv[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout='{"loggedIn":true,"authMethod":"oauth_token"}\n',
                stderr="",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Usage: claude [options] [prompt]\n-p --output-format --json-schema --resume --permission-mode dontAsk --tools --allowedTools --mcp-config --strict-mcp-config --setting-sources --safe-mode --model\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = ClaudeCodeCliChecker()(loaded)

    assert [result.name for result in results] == [
        "claude_code_cli_version",
        "claude_code_auth_status",
        "claude_code_print_capabilities",
    ]
    assert [result.status for result in results] == ["ok", "ok", "ok"]
    assert calls == [
        ["/bin/claude", "--version"],
        ["/bin/claude", "auth", "status"],
        ["/bin/claude", "-p", "--help"],
    ]


def test_claude_code_cli_checker_requires_full_access_bypass_flag(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: claude_code
tool_permissions: full_access
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)

    def fake_run(argv, **kwargs):
        if argv[1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout="2.1.201 (Claude Code)\n", stderr=""
            )
        if argv[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout='{"loggedIn":true,"authMethod":"oauth_token"}\n',
                stderr="",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Usage: claude [options] [prompt]\n-p --output-format --json-schema --resume --permission-mode bypassPermissions --tools --allowedTools --mcp-config --strict-mcp-config --setting-sources --safe-mode\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    results = ClaudeCodeCliChecker()(loaded)

    capabilities = next(
        result for result in results if result.name == "claude_code_print_capabilities"
    )
    assert capabilities.is_critical_failure
    assert capabilities.details["missing_flags"] == ["--dangerously-skip-permissions"]


def test_doctor_warns_when_explicit_agent_skill_is_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  hermes:
    skill_paths:
      - skills/missing
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    store = _initialized_store(tmp_path, loaded)
    client = FakeFeishuClient()
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    explicit_skills = next(
        result for result in results if result.name == "agent_explicit_skills"
    )
    assert explicit_skills.severity == "warning"
    assert explicit_skills.status == "failed"
    assert explicit_skills.details["missing"] == [str(tmp_path / "skills" / "missing")]


def test_doctor_accepts_explicit_agent_skill_directory(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "support"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: support\n---\n# Support\n", encoding="utf-8"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  hermes:
    skill_paths:
      - skills/support
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    store = _initialized_store(tmp_path, loaded)
    client = FakeFeishuClient()
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    explicit_skills = next(
        result for result in results if result.name == "agent_explicit_skills"
    )
    assert explicit_skills.status == "ok"
    assert explicit_skills.details["resolved_hermes_skill_paths"] == [str(skill_dir)]
    assert explicit_skills.details["configured_hermes_skill_names"] == ["support"]


def test_doctor_accepts_explicit_agent_skill_file_path(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "support"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: support\n---\n# Support\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  hermes:
    skill_paths:
      - skills/support/SKILL.md
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    store = _initialized_store(tmp_path, loaded)
    client = FakeFeishuClient()
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    explicit_skills = next(
        result for result in results if result.name == "agent_explicit_skills"
    )
    assert explicit_skills.status == "ok"
    assert explicit_skills.details["resolved_hermes_skill_paths"] == [str(skill_dir)]


def test_doctor_rejects_hermes_skill_without_valid_frontmatter_name(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skills" / "support"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Support\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  hermes:
    skill_paths:
      - skills/support
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    suite = HealthSuite(
        loaded_config=loaded,
        store=_initialized_store(tmp_path, loaded),
        feishu_client=FakeFeishuClient(),
        hermes_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    explicit_skills = next(
        result for result in results if result.name == "agent_explicit_skills"
    )
    assert explicit_skills.status == "failed"
    assert explicit_skills.details["invalid_hermes_skills"] == [str(skill_dir)]


def test_doctor_reports_codex_native_skill_name_as_configured(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: codex
  working_dir: .
  codex:
    skills:
      - docmate
""",
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    suite = HealthSuite(
        loaded_config=loaded,
        store=_initialized_store(tmp_path, loaded),
        feishu_client=FakeFeishuClient(),
        agent_backend_checker=ok_hermes,
        run_id="doctor_1",
    )

    results = suite.run(send_test=False)

    explicit_skills = next(
        result for result in results if result.name == "agent_explicit_skills"
    )
    assert explicit_skills.status == "ok"
    assert explicit_skills.details["configured_skill_names"] == ["docmate"]
    assert "available" not in explicit_skills.message
    assert "loaded" not in explicit_skills.message
