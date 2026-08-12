from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..agent_backend import AgentRunResult

READ_TOOL_NAMES = {"execute_code", "read_file", "search_files", "terminal"}
PATH_ARGUMENT_NAMES = {
    "command",
    "cwd",
    "directory",
    "file_path",
    "path",
    "root",
    "target",
    "workdir",
}
TRACE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
CODEX_SKILL_NAME_PATTERN = re.compile(r"<skill>\s*<name>([^<]+)</name>")


def build_skill_trace(
    *,
    backend_provider: str,
    session_ids: Sequence[str],
    expected_skills: Sequence[str],
    hermes_path: str | None,
    timeout_seconds: int | None,
    dry_run_backend: bool,
    requested_skills: Sequence[str] = (),
    catalog_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    expected = list(dict.fromkeys(expected_skills))
    requested = _safe_trace_names(requested_skills)
    base = {
        "status": "unsupported_backend"
        if dry_run_backend or backend_provider not in {"hermes", "codex"}
        else "unavailable",
        "expected_skills": expected,
        "requested_skills": requested,
        "runtime_loaded_skills": [],
        "requested_not_loaded_skills": requested,
        "missing_skills": expected,
        "unexpected_skills": [],
        "skill_view_calls": 0,
        "repository_reads": [],
        "session_models": [],
        "session_providers": [],
    }
    if dry_run_backend or backend_provider not in {"hermes", "codex"}:
        return base
    unique_session_ids = list(dict.fromkeys(item for item in session_ids if item))
    if not unique_session_ids:
        return base
    if backend_provider == "codex":
        sessions: list[dict[str, Any]] = []
        try:
            for session_id in unique_session_ids:
                sessions.extend(_load_codex_session(session_id))
        except FileNotFoundError:
            return _trace_with_runtime_skills(
                base,
                status="unavailable",
                expected_skills=expected,
                requested_skills=requested,
                runtime_loaded_skills=[],
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return {**base, "status": "export_error", "error_code": "invalid_jsonl"}
        return _summarize_codex_sessions(
            sessions,
            expected_skills=expected,
            requested_skills=requested,
        )

    sessions: list[dict[str, Any]] = []
    for session_id in unique_session_ids:
        result = _export_session(
            hermes_path=hermes_path,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
        )
        if not result.ok:
            return {**base, "status": "export_error", "error_code": _error_code(result)}
        try:
            exported = _parse_export(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return {**base, "status": "export_error", "error_code": "invalid_jsonl"}
        sessions.extend(exported)

    return _summarize_sessions(
        sessions,
        expected_skills=expected,
        requested_skills=requested,
        catalog_paths=catalog_paths,
    )


def _export_session(
    *, hermes_path: str | None, session_id: str, timeout_seconds: int | None
) -> AgentRunResult:
    argv = [
        hermes_path or "hermes",
        "sessions",
        "export",
        "--session-id",
        session_id,
        "--format",
        "jsonl",
        "--redact",
        "-",
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AgentRunResult(
            argv=argv,
            exit_code=None,
            timed_out=True,
            error="session export timed out",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except OSError:
        return AgentRunResult(
            argv=argv,
            exit_code=None,
            error="session export could not start",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    return AgentRunResult(
        argv=argv,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        error=None if completed.returncode == 0 else "session export failed",
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def _parse_export(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("session export rows must be objects")
        rows.append(row)
    if not rows:
        raise ValueError("session export was empty")
    return rows


def _load_codex_session(session_id: str) -> list[dict[str, Any]]:
    safe_session_id = _safe_trace_name(session_id)
    if safe_session_id is None:
        raise ValueError("invalid Codex session id")
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    matches = list(
        codex_home.joinpath("sessions").glob(f"*/*/*/rollout-*-{safe_session_id}.jsonl")
    )
    if len(matches) != 1:
        raise FileNotFoundError("Codex session JSONL not found")
    rows = _parse_export(matches[0].read_text(encoding="utf-8"))
    return rows


def _summarize_codex_sessions(
    sessions: Sequence[dict[str, Any]],
    *,
    expected_skills: Sequence[str],
    requested_skills: Sequence[str],
) -> dict[str, Any]:
    runtime_loaded_skills: list[str] = []
    models: list[Any] = []
    providers: list[Any] = []
    for row in sessions:
        row_type = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if row_type == "session_meta":
            providers.append(payload.get("model_provider"))
        elif row_type == "turn_context":
            models.append(payload.get("model"))
        elif row_type == "response_item":
            _collect_codex_skill_names(payload, runtime_loaded_skills)
    trace = _trace_with_runtime_skills(
        {},
        status="available",
        expected_skills=expected_skills,
        requested_skills=requested_skills,
        runtime_loaded_skills=runtime_loaded_skills,
    )
    return trace | {
        "skill_view_calls": 0,
        "repository_reads": [],
        "session_models": _unique_strings(models),
        "session_providers": _unique_strings(providers),
    }


def _collect_codex_skill_names(
    payload: dict[str, Any], loaded_skills: list[str]
) -> None:
    if payload.get("type") != "message" or payload.get("role") != "user":
        return
    for item in payload.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "input_text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        stripped = text.lstrip()
        if not stripped.startswith("<skill>"):
            continue
        for match in CODEX_SKILL_NAME_PATTERN.finditer(stripped):
            name = _safe_trace_name(match.group(1))
            if name and name not in loaded_skills:
                loaded_skills.append(name)


def _summarize_sessions(
    sessions: Sequence[dict[str, Any]],
    *,
    expected_skills: Sequence[str],
    requested_skills: Sequence[str],
    catalog_paths: Sequence[Path] | None,
) -> dict[str, Any]:
    runtime_loaded_skills: list[str] = []
    skill_view_calls = 0
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    for session in sessions:
        for message in session.get("messages") or []:
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                parsed = _parse_tool_call(call)
                if parsed is None:
                    continue
                name, arguments = parsed
                tool_calls.append(parsed)
                if name != "skill_view":
                    continue
                skill_view_calls += 1
                skill_name = arguments.get("name")
                if isinstance(skill_name, str):
                    normalized = _safe_trace_name(skill_name)
                    if normalized and normalized not in runtime_loaded_skills:
                        runtime_loaded_skills.append(normalized)
    expected = list(expected_skills)
    trace = _trace_with_runtime_skills(
        {},
        status="available",
        expected_skills=expected,
        requested_skills=requested_skills,
        runtime_loaded_skills=runtime_loaded_skills,
    )
    return trace | {
        "skill_view_calls": skill_view_calls,
        "repository_reads": _repository_reads(
            tool_calls,
            catalog_paths=_default_catalog_paths()
            if catalog_paths is None
            else catalog_paths,
        ),
        "session_models": _unique_strings(session.get("model") for session in sessions),
        "session_providers": _unique_strings(
            session.get("billing_provider") or session.get("provider")
            for session in sessions
        ),
    }


def _trace_with_runtime_skills(
    base: dict[str, Any],
    *,
    status: str,
    expected_skills: Sequence[str],
    requested_skills: Sequence[str],
    runtime_loaded_skills: Sequence[str],
) -> dict[str, Any]:
    requested = list(dict.fromkeys(requested_skills))
    runtime_loaded = list(dict.fromkeys(runtime_loaded_skills))
    expected = list(expected_skills)
    return base | {
        "status": status,
        "expected_skills": expected,
        "requested_skills": requested,
        "runtime_loaded_skills": runtime_loaded,
        "requested_not_loaded_skills": [
            item for item in requested if item not in runtime_loaded
        ],
        "missing_skills": [item for item in expected if item not in runtime_loaded],
        "unexpected_skills": [item for item in runtime_loaded if item not in expected],
    }


def _parse_tool_call(call: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(call, dict):
        return None
    function = call.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_arguments = function.get("arguments")
    if isinstance(raw_arguments, dict):
        arguments = raw_arguments
    elif isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            parsed = {}
        arguments = parsed if isinstance(parsed, dict) else {}
    else:
        arguments = {}
    return name.rsplit(".", 1)[-1], arguments


def _repository_reads(
    tool_calls: Sequence[tuple[str, dict[str, Any]]],
    *,
    catalog_paths: Sequence[Path],
) -> list[dict[str, Any]]:
    repositories = _load_repositories(catalog_paths)
    found: dict[str, set[str]] = {}
    for tool_name, arguments in tool_calls:
        if tool_name not in READ_TOOL_NAMES:
            continue
        workdir = _argument_path(arguments, "workdir", "cwd")
        values = list(_path_argument_values(arguments))
        if tool_name in {"terminal", "execute_code"}:
            values.extend(_command_tokens(arguments.get("command")))
        for repository, root in repositories:
            relative_paths = _paths_for_repository(
                values,
                workdir=workdir,
                root=root,
            )
            if relative_paths:
                found.setdefault(repository, set()).update(relative_paths)
    return [
        {"repository": repository, "paths": sorted(paths)}
        for repository, paths in sorted(found.items())
    ]


def _load_repositories(catalog_paths: Sequence[Path]) -> list[tuple[str, Path]]:
    repositories: list[tuple[str, Path]] = []
    for catalog_path in catalog_paths:
        try:
            data = json.loads(catalog_path.expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get("repos") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            path = item.get("path")
            safe_name = _safe_trace_name(name) if isinstance(name, str) else None
            if safe_name is not None and isinstance(path, str) and path:
                repositories.append((safe_name, Path(path).expanduser()))
    repositories.sort(key=lambda item: len(str(item[1])), reverse=True)
    return repositories


def _path_argument_values(value: Any, *, key: str | None = None) -> Iterable[str]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _path_argument_values(child, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _path_argument_values(child, key=key)
    elif isinstance(value, str) and key in PATH_ARGUMENT_NAMES:
        yield value


def _argument_path(arguments: dict[str, Any], *keys: str) -> Path | None:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    return None


def _command_tokens(command: Any) -> list[str]:
    if not isinstance(command, str):
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _paths_for_repository(
    values: Sequence[str], *, workdir: Path | None, root: Path
) -> set[str]:
    paths: set[str] = set()
    for value in values:
        candidate = Path(value).expanduser()
        resolved_candidate = candidate if candidate.is_absolute() else None
        if resolved_candidate is None and workdir is not None:
            resolved_candidate = workdir / candidate
        if resolved_candidate is None:
            continue
        try:
            exists = resolved_candidate.exists()
        except (OSError, ValueError):
            exists = False
        if not exists:
            continue
        relative = _relative_to(resolved_candidate, root)
        if relative is not None:
            paths.add(relative)
    return paths


def _relative_to(path: Path, root: Path) -> str | None:
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return None
    return "." if str(relative) == "." else relative.as_posix()


def _unique_strings(values: Iterable[Any]) -> list[str]:
    return list(
        dict.fromkeys(value for value in values if isinstance(value, str) and value)
    )


def _safe_trace_name(value: str) -> str | None:
    normalized = value.strip()
    if TRACE_NAME_PATTERN.fullmatch(normalized) is None or ".." in normalized:
        return None
    return normalized


def _safe_trace_names(values: Sequence[str]) -> list[str]:
    return list(
        dict.fromkeys(
            normalized
            for value in values
            if (normalized := _safe_trace_name(value)) is not None
        )
    )


def _default_catalog_paths() -> list[Path]:
    references = Path.home() / ".agents" / "skills" / "docmate" / "references"
    return [
        references / "docmate.primary.catalog.json",
        references / "docmate.fallback.catalog.json",
    ]


def _error_code(result: AgentRunResult) -> str:
    if result.timed_out:
        return "timeout"
    if result.exit_code is None:
        return "unavailable"
    return "command_failed"
