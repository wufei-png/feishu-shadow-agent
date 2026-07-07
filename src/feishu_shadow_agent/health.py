from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .agent_backend import AgentRunResult
from .config import LoadedConfig
from .feishu.client import FeishuClient
from .hermes import hermes_execution_policy
from .paths import (
    resolve_agent_skill_path,
    resolve_agent_working_dir,
    resolve_relative_path,
)
from .store.sqlite_store import SQLiteStore
from .types import HealthCheckResult, LarkCliResult, new_run_id

REQUIRED_USER_SCOPES = {
    "search:message",
    "im:message:readonly",
    "im:chat:read",
    "im:message.p2p_msg:get_as_user",
    "im:message.group_msg:get_as_user",
    "im:message.reactions:read",
    "im:message.send_as_user",
    "im:message",
}

BackendReadinessChecker = Callable[
    [LoadedConfig], HealthCheckResult | list[HealthCheckResult]
]
HermesChecker = BackendReadinessChecker


class HermesHttpChecker:
    def __call__(self, loaded: LoadedConfig) -> HealthCheckResult:
        hermes = loaded.config.agent_backend.hermes
        if not hermes.health_url:
            return HealthCheckResult(
                "hermes_reachable",
                "critical",
                "failed",
                "agent_backend.hermes.health_url is not configured",
            )
        headers = {}
        if hermes.api_key_env:
            api_key = os.environ.get(hermes.api_key_env)
            if not api_key:
                return HealthCheckResult(
                    "hermes_reachable",
                    "critical",
                    "failed",
                    f"environment variable {hermes.api_key_env} is not set",
                )
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            hermes.health_url, headers=headers, method="GET"
        )
        try:
            with urllib.request.urlopen(
                request, timeout=hermes.timeout_seconds
            ) as response:
                status = response.status
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return HealthCheckResult(
                "hermes_reachable",
                "critical",
                "failed",
                "Hermes health check failed",
                {"error": str(exc)},
            )
        if 200 <= status < 400:
            return HealthCheckResult(
                "hermes_reachable",
                "critical",
                "ok",
                "Hermes health endpoint is reachable",
                {"status": status},
            )
        return HealthCheckResult(
            "hermes_reachable",
            "critical",
            "failed",
            "Hermes health endpoint returned non-success",
            {"status": status},
        )


class HermesCliChecker:
    def __call__(self, loaded: LoadedConfig) -> list[HealthCheckResult]:
        hermes = loaded.config.agent_backend.hermes
        path = hermes.path or "hermes"
        cwd = resolve_agent_working_dir(
            loaded.config.agent_backend.working_dir, loaded.base_dir
        )
        version = _run_hermes_command(
            [path, "--version"], timeout_seconds=hermes.timeout_seconds, cwd=cwd
        )
        version_result = _hermes_command_result(
            "hermes_cli_version",
            version,
            "Hermes CLI version detected",
            "Hermes CLI version check failed",
            severity="critical",
        )
        if version_result.is_critical_failure:
            return [version_result]
        status = _run_hermes_command(
            [path, "status"], timeout_seconds=hermes.timeout_seconds, cwd=cwd
        )
        status_result = _hermes_command_result(
            "hermes_cli_status",
            status,
            "Hermes CLI status command succeeded",
            "Hermes CLI status command failed",
            severity="warning",
        )
        chat_help = _run_hermes_command(
            [path, "chat", "--help"], timeout_seconds=hermes.timeout_seconds, cwd=cwd
        )
        permissions_result = _hermes_tool_permissions_result(loaded, chat_help)
        return [version_result, status_result, permissions_result]


class HermesHealthChecker:
    def __init__(
        self,
        *,
        cli_checker: HermesChecker | None = None,
        http_checker: HermesChecker | None = None,
    ):
        self.cli_checker = cli_checker or HermesCliChecker()
        self.http_checker = http_checker or HermesHttpChecker()

    def __call__(self, loaded: LoadedConfig) -> list[HealthCheckResult]:
        cli_results = _health_result_list(self.cli_checker(loaded))
        if loaded.config.agent_backend.hermes.mode != "http":
            return cli_results
        return [*cli_results, *_health_result_list(self.http_checker(loaded))]


class SelectedBackendReadinessChecker:
    def __init__(self, *, hermes_checker: HermesChecker | None = None):
        self.hermes_checker = hermes_checker or HermesHealthChecker()

    def __call__(self, loaded: LoadedConfig) -> list[HealthCheckResult]:
        provider = loaded.config.agent_backend.provider
        if provider == "hermes":
            return _health_result_list(self.hermes_checker(loaded))
        return [
            HealthCheckResult(
                "agent_backend_ready",
                "critical",
                "failed",
                f"unsupported agent backend provider: {provider}",
                {"provider": provider},
            )
        ]


class HealthSuite:
    def __init__(
        self,
        *,
        loaded_config: LoadedConfig,
        store: SQLiteStore,
        feishu_client: FeishuClient,
        agent_backend_checker: BackendReadinessChecker | None = None,
        hermes_checker: HermesChecker | None = None,
        run_id: str | None = None,
    ):
        if agent_backend_checker is not None and hermes_checker is not None:
            raise ValueError("configure either agent_backend_checker or hermes_checker")
        self.loaded_config = loaded_config
        self.store = store
        self.feishu_client = feishu_client
        self.agent_backend_checker = agent_backend_checker or (
            SelectedBackendReadinessChecker(hermes_checker=hermes_checker)
        )
        self.run_id = run_id

    def run(self, *, send_test: bool = False) -> list[HealthCheckResult]:
        results: list[HealthCheckResult] = [
            self._check_config_schema(),
            self._check_sqlite_writable(),
            self._check_product_policy_initialized(),
            self._check_lark_cli_version(),
            self._check_agent_working_dir(),
        ]
        auth_result = self._check_auth_status()
        results.append(auth_result)
        auth_json = (
            auth_result.details.get("auth_json") if auth_result.details else None
        )
        results.append(self._check_user_scopes(auth_json))
        results.append(self._check_bot_available(auth_json))
        results.append(self._check_owner_config())
        results.append(self._check_owner_notification(send_test=send_test))
        backend_results = self.agent_backend_checker(self.loaded_config)
        if isinstance(backend_results, list):
            results.extend(backend_results)
        else:
            results.append(backend_results)
        results.append(self._check_agent_explicit_skills())
        results.extend(self._check_reply_postprocess_guidance())
        self.store.record_health_results(run_id=self.run_id, results=results)
        return results

    def run_runtime_critical(self) -> list[HealthCheckResult]:
        results: list[HealthCheckResult] = [
            self._check_config_schema(),
            self._check_sqlite_writable(),
            self._check_product_policy_initialized(),
            self._check_lark_cli_version(),
            self._check_agent_working_dir(),
        ]
        auth_result = self._check_auth_status()
        results.append(auth_result)
        auth_json = (
            auth_result.details.get("auth_json") if auth_result.details else None
        )
        results.append(self._check_user_scopes(auth_json))
        results.append(self._check_bot_available(auth_json))
        results.append(self._check_owner_config())
        backend_results = self.agent_backend_checker(self.loaded_config)
        if isinstance(backend_results, list):
            results.extend(backend_results)
        else:
            results.append(backend_results)
        self.store.record_health_results(run_id=self.run_id, results=results)
        return results

    def _check_config_schema(self) -> HealthCheckResult:
        return HealthCheckResult(
            "config_schema",
            "critical",
            "ok",
            "config schema is valid",
            {"path": str(self.loaded_config.path)},
        )

    def _check_sqlite_writable(self) -> HealthCheckResult:
        try:
            self.store.migrate()
            self.store.health_probe()
        except Exception as exc:  # pragma: no cover - platform-specific detail
            return HealthCheckResult(
                "sqlite_writable",
                "critical",
                "failed",
                "SQLite is not writable",
                {"error": str(exc)},
            )
        return HealthCheckResult(
            "sqlite_writable",
            "critical",
            "ok",
            "SQLite is writable",
            {"path": str(self.store.path)},
        )

    def _check_product_policy_initialized(self) -> HealthCheckResult:
        try:
            probe = self.store.product_policy_initialization_probe()
        except Exception as exc:  # pragma: no cover - platform-specific detail
            return HealthCheckResult(
                "product_policy_initialized",
                "critical",
                "failed",
                "Product Policy Store initialization check failed",
                {"error": str(exc), "path": str(self.store.path)},
            )
        if not probe["initialized"]:
            return HealthCheckResult(
                "product_policy_initialized",
                "critical",
                "failed",
                "Product Policy Store global policy is not initialized; run `policy import-config`.",
                {"missing": probe["missing"], "path": str(self.store.path)},
            )
        return HealthCheckResult(
            "product_policy_initialized",
            "critical",
            "ok",
            "Product Policy Store global policy is initialized",
            {"path": str(self.store.path)},
        )

    def _check_lark_cli_version(self) -> HealthCheckResult:
        configured_path = self.loaded_config.config.lark_cli.path
        resolved_path = None
        if configured_path:
            resolved_path = _resolve_executable_path(configured_path)
            if not resolved_path or not os.path.exists(resolved_path):
                return HealthCheckResult(
                    "lark_cli_version",
                    "critical",
                    "failed",
                    "configured lark-cli path was not found",
                    {"path": configured_path},
                )
        result = self.feishu_client.version()
        if not result.ok:
            return _command_failed(
                "lark_cli_version", result, "lark-cli version check failed"
            )
        argv_path = result.argv[0] if result.argv else configured_path
        resolved_path = resolved_path or _resolve_executable_path(argv_path)
        return HealthCheckResult(
            "lark_cli_version",
            "critical",
            "ok",
            "lark-cli version detected",
            {
                "stdout": result.stdout.strip(),
                "argv": result.argv,
                "configured_path": configured_path,
                "resolved_path": resolved_path,
            },
        )

    def _check_agent_working_dir(self) -> HealthCheckResult:
        configured = self.loaded_config.config.agent_backend.working_dir
        resolved = resolve_agent_working_dir(configured, self.loaded_config.base_dir)
        details = {
            "configured": configured,
            "resolved": str(resolved),
        }
        if not resolved.exists():
            return HealthCheckResult(
                "agent_working_dir",
                "critical",
                "failed",
                "agent working directory does not exist",
                details,
            )
        if not resolved.is_dir():
            return HealthCheckResult(
                "agent_working_dir",
                "critical",
                "failed",
                "agent working directory is not a directory",
                details,
            )
        return HealthCheckResult(
            "agent_working_dir",
            "critical",
            "ok",
            "agent working directory is available",
            details,
        )

    def _check_auth_status(self) -> HealthCheckResult:
        result = self.feishu_client.auth_status(verify=True)
        if not result.ok:
            return _command_failed(
                "lark_auth_verify", result, "lark-cli auth verify failed"
            )
        return HealthCheckResult(
            "lark_auth_verify",
            "critical",
            "ok",
            "lark-cli auth verify succeeded",
            {"auth_json": result.json_data, "argv": result.argv},
        )

    def _check_user_scopes(self, auth_json: object) -> HealthCheckResult:
        scope_text = ""
        if isinstance(auth_json, dict):
            user = auth_json.get("identities", {}).get("user", {})
            if isinstance(user, dict):
                scope_text = str(user.get("scope") or "")
        if not scope_text:
            return HealthCheckResult(
                "required_user_scopes",
                "critical",
                "failed",
                "auth status did not expose user scopes",
            )
        granted = set(scope_text.split())
        missing = sorted(REQUIRED_USER_SCOPES - granted)
        if missing:
            return HealthCheckResult(
                "required_user_scopes",
                "critical",
                "failed",
                "required user scopes are missing",
                {"missing": missing},
            )
        return HealthCheckResult(
            "required_user_scopes",
            "critical",
            "ok",
            "required user scopes are present",
        )

    def _check_bot_available(self, auth_json: object) -> HealthCheckResult:
        bot = {}
        if isinstance(auth_json, dict):
            maybe_bot = auth_json.get("identities", {}).get("bot", {})
            if isinstance(maybe_bot, dict):
                bot = maybe_bot
        bot_open_id = bot.get("openId") or bot.get("open_id") or bot.get("id")
        if (
            bot.get("available") is True or bot.get("status") == "ready"
        ) and bot_open_id:
            return HealthCheckResult(
                "bot_identity",
                "critical",
                "ok",
                "bot identity is available",
                {"status": bot.get("status"), "open_id": bot_open_id},
            )
        return HealthCheckResult(
            "bot_identity",
            "critical",
            "failed",
            "bot identity is not available",
            {
                "status": bot.get("status"),
                "message": bot.get("message"),
                "open_id": bot_open_id,
            },
        )

    def _check_owner_config(self) -> HealthCheckResult:
        owner = self.loaded_config.config.owner
        return HealthCheckResult(
            "owner_config",
            "critical",
            "ok",
            "owner open_id is configured",
            {"open_id": owner.open_id, "name": owner.name},
        )

    def _check_owner_notification(self, *, send_test: bool) -> HealthCheckResult:
        owner = self.loaded_config.config.owner
        result = self.feishu_client.owner_message(
            owner_open_id=owner.open_id,
            text="feishu-shadow-agent doctor test",
            idempotency_key=f"doctor_{new_run_id('owner')}",
            dry_run=not send_test,
        )
        name = "owner_notification_send" if send_test else "owner_notification_dry_run"
        if not result.ok:
            return _command_failed(
                name, result, "owner notification check failed", severity="warning"
            )
        return HealthCheckResult(
            name,
            "warning",
            "ok",
            "owner notification command succeeded",
            {"dry_run": not send_test, "argv": result.argv},
        )

    def _check_agent_explicit_skills(self) -> HealthCheckResult:
        skills = self.loaded_config.config.agent_backend.explicit_context.skills
        if not skills:
            return HealthCheckResult(
                "agent_explicit_skills",
                "warning",
                "ok",
                "no explicit agent skills configured",
            )
        missing: list[str] = []
        invalid: list[str] = []
        resolved: list[str] = []
        for skill in skills:
            path = resolve_agent_skill_path(skill, self.loaded_config.base_dir)
            resolved.append(str(path))
            if not path.exists():
                missing.append(str(path))
                continue
            if not path.is_dir() or not (path / "SKILL.md").is_file():
                invalid.append(str(path))
        if missing or invalid:
            return HealthCheckResult(
                "agent_explicit_skills",
                "warning",
                "failed",
                "some explicit agent skills are missing or invalid",
                {
                    "configured": skills,
                    "resolved": resolved,
                    "missing": missing,
                    "invalid": invalid,
                },
            )
        return HealthCheckResult(
            "agent_explicit_skills",
            "warning",
            "ok",
            "explicit agent skills are present",
            {"configured": skills, "resolved": resolved},
        )

    def _check_reply_postprocess_guidance(self) -> list[HealthCheckResult]:
        cfg = self.loaded_config.config.reply_postprocess
        if not cfg.enabled:
            return []
        results: list[HealthCheckResult] = []
        if cfg.owner_style.enabled:
            path = resolve_relative_path(
                cfg.owner_style.profile_path, self.loaded_config.base_dir
            )
            results.append(
                _readable_file_result(
                    name="reply_postprocess_owner_style_profile",
                    path=path,
                    configured=cfg.owner_style.profile_path,
                    ok_message="reply postprocess owner style profile is readable",
                    failed_message="reply postprocess owner style profile is missing or unreadable",
                )
            )
        if cfg.humanizer_zh.enabled:
            path = resolve_relative_path(
                cfg.humanizer_zh.skill_path, self.loaded_config.base_dir
            )
            results.append(
                _readable_file_result(
                    name="reply_postprocess_humanizer_zh_skill",
                    path=path,
                    configured=cfg.humanizer_zh.skill_path,
                    ok_message="reply postprocess humanizer-zh skill is readable",
                    failed_message="reply postprocess humanizer-zh skill is missing or unreadable",
                )
            )
        return results


def has_critical_failure(results: list[HealthCheckResult]) -> bool:
    return any(result.is_critical_failure for result in results)


def _health_result_list(
    result: HealthCheckResult | list[HealthCheckResult],
) -> list[HealthCheckResult]:
    return result if isinstance(result, list) else [result]


def summarize_results(results: list[HealthCheckResult]) -> dict[str, object]:
    return {
        "critical_failed": [
            result.name for result in results if result.is_critical_failure
        ],
        "critical_messages": {
            result.name: result.message
            for result in results
            if result.is_critical_failure
        },
        "warnings": [
            result.name
            for result in results
            if result.severity == "warning" and result.status != "ok"
        ],
    }


def _command_failed(
    name: str,
    result: LarkCliResult,
    message: str,
    *,
    severity: str = "critical",
) -> HealthCheckResult:
    return HealthCheckResult(
        name,
        severity,  # type: ignore[arg-type]
        "failed",
        message,
        {
            "argv": result.argv,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.error,
            "timed_out": result.timed_out,
        },
    )


def _run_hermes_command(
    argv: list[str], *, timeout_seconds: int, cwd: Path | None = None
) -> AgentRunResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return AgentRunResult(
            argv=argv,
            exit_code=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            error=f"command timed out after {timeout_seconds}s",
            timed_out=True,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except OSError as exc:
        return AgentRunResult(
            argv=argv,
            exit_code=None,
            error=str(exc),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    return AgentRunResult(
        argv=argv,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        error=None
        if completed.returncode == 0
        else (completed.stderr.strip() or completed.stdout.strip()),
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def _hermes_command_result(
    name: str,
    result: AgentRunResult,
    ok_message: str,
    failed_message: str,
    *,
    severity: str,
) -> HealthCheckResult:
    if not result.ok:
        return HealthCheckResult(
            name,
            severity,  # type: ignore[arg-type]
            "failed",
            failed_message,
            {
                "argv": result.argv,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "error": result.error,
                "timed_out": result.timed_out,
                "latency_ms": result.latency_ms,
            },
        )
    return HealthCheckResult(
        name,
        severity,  # type: ignore[arg-type]
        "ok",
        ok_message,
        {
            "argv": result.argv,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "latency_ms": result.latency_ms,
        },
    )


def _hermes_tool_permissions_result(
    loaded: LoadedConfig, result: AgentRunResult
) -> HealthCheckResult:
    profile = loaded.config.tool_permissions
    policy = hermes_execution_policy(profile)
    backend = loaded.config.agent_backend
    details = {
        "tool_permissions_profile": profile,
        "effective_args": policy.cli_args(),
        "config_scope": backend.config_scope,
        "auto_context": backend.auto_context,
        "explicit_skills_count": len(backend.explicit_context.skills),
        "argv": result.argv,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "exit_code": result.exit_code,
        "error": result.error,
        "timed_out": result.timed_out,
    }
    if not result.ok:
        return HealthCheckResult(
            "hermes_tool_permissions",
            "critical",
            "failed",
            "Hermes chat help command failed",
            details,
        )
    help_text = f"{result.stdout}\n{result.stderr}"
    required_flags = ["--toolsets", *_required_hermes_backend_flags(loaded)]
    missing = [flag for flag in required_flags if flag not in help_text]
    if policy.yolo and "--yolo" not in help_text:
        missing.append("--yolo")
    if missing:
        return HealthCheckResult(
            "hermes_tool_permissions",
            "critical",
            "failed",
            "Hermes CLI does not expose required backend flags",
            details | {"missing_flags": missing},
        )
    return HealthCheckResult(
        "hermes_tool_permissions",
        "critical",
        "ok",
        "Hermes CLI supports configured backend flags",
        details,
    )


def _required_hermes_backend_flags(loaded: LoadedConfig) -> list[str]:
    backend = loaded.config.agent_backend
    hermes = backend.hermes
    flags: list[str] = []
    if backend.config_scope == "isolated":
        flags.append("--ignore-user-config")
    if backend.auto_context == "disabled":
        flags.append("--ignore-rules")
    if backend.explicit_context.skills:
        flags.append("--skills")
    if hermes.model:
        flags.append("--model")
    if hermes.provider:
        flags.append("--provider")
    return flags


def _resolve_executable_path(path: str | None) -> str | None:
    if not path:
        return None
    if os.path.isabs(path):
        return path
    return shutil.which(path)


def _readable_file_result(
    *,
    name: str,
    path: Path,
    configured: str,
    ok_message: str,
    failed_message: str,
) -> HealthCheckResult:
    details = {"configured": configured, "resolved": str(path)}
    if path.is_file() and os.access(path, os.R_OK):
        return HealthCheckResult(name, "warning", "ok", ok_message, details)
    return HealthCheckResult(name, "warning", "failed", failed_message, details)
