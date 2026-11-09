from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..config import LoadedConfig
from ..paths import resolve_relative_path
from .artifacts import EvalError, read_yaml


def configured_group_ids(loaded: LoadedConfig) -> set[str]:
    """Read the effective policy without creating or migrating a production database."""
    database = resolve_relative_path(loaded.config.storage.sqlite_path, loaded.base_dir)
    if not database.is_file():
        return set()
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
            return {
                str(row[0]) for row in conn.execute("SELECT chat_id FROM chat_policies")
            }
    except sqlite3.Error as exc:
        raise EvalError(f"cannot read effective group policies: {exc}") from exc


def require_configured_group(loaded: LoadedConfig, chat_id: str) -> None:
    if not chat_id or chat_id not in configured_group_ids(loaded):
        raise EvalError(f"group policy is not configured: {chat_id or '<unknown>'}")


def validate_model_messages(
    loaded: LoadedConfig, raws: list[dict[str, Any]], *, directory: Path
) -> set[str]:
    """Require an identified source unless the case is explicitly synthetic."""
    metadata = directory / "metadata.yaml"
    synthetic = (
        metadata.is_file() and read_yaml(metadata).get("source_kind") == "synthetic"
    )
    groups: set[str] = set()
    for raw in raws:
        chat_type = raw.get("chat_type") or raw.get("chatType")
        chat_id = raw.get("chat_id") or raw.get("chatId")
        if (
            not isinstance(chat_id, str)
            or not chat_id
            or chat_type not in {"group", "p2p"}
        ):
            if synthetic:
                continue
            raise EvalError(
                "evaluation message source is unknown; mark synthetic fixtures explicitly"
            )
        if chat_type == "group":
            groups.add(chat_id)
    if not synthetic:
        configured = configured_group_ids(loaded)
        missing = groups - configured
        if missing:
            raise EvalError(
                f"group policy is not configured for evaluation: {sorted(missing)}"
            )
    return groups
