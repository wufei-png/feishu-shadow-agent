from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from ..config import LoadedConfig
from ..types import utc_now_iso


class EvalError(ValueError):
    pass


LABEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
SECRET_KEY_MARKERS = (
    "secret",
    "token",
    "password",
    "api_key",
    "private_key",
    "webhook",
    "cookie",
)


def evals_base_dir(loaded: LoadedConfig) -> Path:
    return loaded.base_dir / "data" / "evals"


def make_run_id(prefix: str, label: str | None = None) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = f"{timestamp}-{secrets.token_hex(3)}"
    if label:
        validate_label(label)
        return f"{prefix}-{suffix}-{label}"
    return f"{prefix}-{suffix}"


def reserve_run_dir(
    root: Path, prefix: str, label: str | None = None
) -> tuple[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        run_id = make_run_id(prefix, label)
        run_dir = root / run_id
        try:
            run_dir.mkdir()
        except FileExistsError:
            continue
        return run_id, run_dir
    raise EvalError(f"could not reserve unique run directory under {root}")


def validate_label(value: str) -> None:
    if not value or not LABEL_PATTERN.fullmatch(value):
        raise EvalError("label must match [A-Za-z0-9._-]+")


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EvalError(f"failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvalError(f"{path} must contain a YAML mapping")
    return cast(dict[str, Any], data)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"failed to read JSON {path}: {exc}") from exc


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rows.append(json.loads(stripped))
                except json.JSONDecodeError as exc:
                    raise EvalError(
                        f"invalid JSONL at {path}:{line_number}: {exc}"
                    ) from exc
    except OSError as exc:
        raise EvalError(f"failed to read {path}: {exc}") from exc
    return rows


def copy_config_or_raise(
    *,
    loaded: LoadedConfig,
    destination_dir: Path,
    allow_sensitive_config: bool,
) -> dict[str, Any]:
    sensitive_paths = validate_config_copy(
        loaded=loaded, allow_sensitive_config=allow_sensitive_config
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(loaded.path, destination_dir / "config.yaml")
    return {
        "config_hash": file_sha256(loaded.path),
        "config_base_dir": str(loaded.base_dir),
        "config_contains_sensitive_fields": bool(sensitive_paths),
        "sensitive_config_paths": sensitive_paths,
    }


def validate_config_copy(
    *, loaded: LoadedConfig, allow_sensitive_config: bool
) -> list[str]:
    sensitive_paths = sensitive_config_paths(loaded.raw)
    if sensitive_paths and not allow_sensitive_config:
        joined = ", ".join(sensitive_paths)
        raise EvalError(
            "config.yaml appears to contain sensitive fields. "
            f"Re-run with --allow-sensitive-config to copy it anyway: {joined}"
        )
    return sensitive_paths


def write_metadata(
    directory: Path,
    *,
    loaded: LoadedConfig,
    config_info: dict[str, Any],
    lark_cli_version: str | None = None,
    prompt_hashes: dict[str, str] | None = None,
    prompt_versions: dict[str, str] | None = None,
) -> None:
    metadata = {
        "schema_version": "eval_metadata_v1",
        "created_at": utc_now_iso(),
        "git_commit": git_output(
            ["git", "rev-parse", "--short", "HEAD"], loaded.base_dir
        ),
        "git_dirty": bool(
            git_output(["git", "status", "--porcelain"], loaded.base_dir)
        ),
        **config_info,
        "prompt_hashes": prompt_hashes or {},
        "prompt_versions": prompt_versions or {},
        "agent_backend": model_metadata(loaded),
        "lark_cli_version": lark_cli_version,
        "contains_private_data": True,
    }
    write_yaml(directory / "metadata.yaml", metadata)


def model_metadata(loaded: LoadedConfig) -> dict[str, Any]:
    backend = loaded.config.agent_backend
    selected = getattr(backend, backend.provider)
    return {
        "backend": backend.provider,
        "model": selected.model,
        "model_provider": backend.hermes.provider
        if backend.provider == "hermes"
        else None,
        "tool_permissions": loaded.config.tool_permissions,
    }


def sensitive_config_paths(raw: Any) -> list[str]:
    paths: list[str] = []

    def visit(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            mapping = cast(dict[str, Any], value)
            for key, child in mapping.items():
                key_text = str(key)
                lowered = key_text.lower()
                child_path = (*path, key_text)
                if (
                    child not in (None, "", [], {})
                    and not lowered.endswith("_env")
                    and any(marker in lowered for marker in SECRET_KEY_MARKERS)
                ):
                    paths.append(".".join(child_path))
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(cast(list[Any], value)):
                visit(child, (*path, str(index)))

    visit(raw, ())
    return sorted(set(paths))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def git_output(argv: list[str], cwd: Path) -> str | None:
    try:
        # This helper invokes fixed Git subcommands with shell=False.
        completed = subprocess.run(  # noqa: S603
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def message_id_from_raw(raw: dict[str, Any]) -> str:
    for key in ("message_id", "messageId", "id"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def text_excerpt(value: str, *, limit: int = 120) -> str:
    cleaned = " ".join(str(value).split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."
