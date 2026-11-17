from __future__ import annotations

import fcntl
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .config import ConfigError, LoadedConfig
from .paths import resolve_relative_path
from .store.schema import (
    IncompatibleSchemaError,
    is_uninitialized_schema,
    require_current_schema,
)
from .store.sqlite_store import PRODUCT_POLICY_KEY, SQLiteStore


def dry_run_config(loaded: LoadedConfig) -> LoadedConfig:
    """Keep application-owned files separate while retaining config-relative paths."""
    config = loaded.config.model_copy(deep=True)
    resource = Path(config.storage.resource_dir)
    if not resource.name:
        raise ConfigError(
            "dry-run requires storage.resource_dir to name a directory below the config directory"
        )
    isolated_resource = resource.with_name(resource.name + ".dry-run")
    original_root = resolve_relative_path(resource, loaded.base_dir).resolve()
    isolated_root = resolve_relative_path(isolated_resource, loaded.base_dir).resolve()
    if (
        original_root == isolated_root
        or original_root in isolated_root.parents
        or isolated_root in original_root.parents
    ):
        raise ConfigError("dry-run and production resource directories overlap")
    config.storage.sqlite_path = _isolated_name(config.storage.sqlite_path)
    config.storage.resource_dir = str(isolated_resource)
    config.logging.jsonl_path = _isolated_name(config.logging.jsonl_path)
    if config.logging.text_path is not None:
        config.logging.text_path = _isolated_name(config.logging.text_path)
    production_files = [
        loaded.config.storage.sqlite_path,
        loaded.config.logging.jsonl_path,
    ]
    dry_files = [config.storage.sqlite_path, config.logging.jsonl_path]
    if (
        loaded.config.logging.text_path is not None
        and config.logging.text_path is not None
    ):
        production_files.append(loaded.config.logging.text_path)
        dry_files.append(config.logging.text_path)
    prod_paths = {
        resolve_relative_path(value, loaded.base_dir).resolve()
        for value in production_files
    }
    dry_paths = {
        resolve_relative_path(value, loaded.base_dir).resolve() for value in dry_files
    }
    if (
        prod_paths & dry_paths
        or any(
            original_root == path or original_root in path.parents for path in dry_paths
        )
        or any(
            isolated_root == path or isolated_root in path.parents
            for path in prod_paths
        )
    ):
        raise ConfigError("dry-run and production data or log paths overlap")
    return replace(loaded, config=config)


def _isolated_name(value: str) -> str:
    path = Path(value)
    if not path.name:
        raise ConfigError(f"cannot derive a dry-run path from {value!r}")
    return str(path.with_name(path.name + ".dry-run"))


def initialize_dry_run_policy(loaded: LoadedConfig, store: SQLiteStore) -> str:
    """Snapshot production policy at most once; never copy its runtime records."""
    with _policy_lock(store):
        store.initialize()
        marker = store.get_checkpoint("dry_run.policy_initialized")
        if marker is not None:
            return str(marker.get("source") or "existing")
        snapshot = _read_production_policy(loaded)
        if snapshot is None:
            source = (
                "existing"
                if store.get_product_policy() is not None
                else "uninitialized"
            )
            store.set_checkpoint("dry_run.policy_initialized", {"source": source})
        else:
            source = "production"
            _apply_snapshot(store, *snapshot, source=source)
        return source


def sync_production_policy(loaded: LoadedConfig, store: SQLiteStore) -> dict[str, Any]:
    with _policy_lock(store):
        snapshot = _read_production_policy(loaded)
        if snapshot is None:
            raise ConfigError(
                "production policy is not initialized; import config into dry-run instead"
            )
        _apply_snapshot(store, *snapshot, source="production_sync")
        return {"source": "production", "chat_count": len(snapshot[1])}


@contextmanager
def _policy_lock(store: SQLiteStore) -> Generator[None, None, None]:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store.path.with_name(store.path.name + ".policy-init.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_production_policy(
    loaded: LoadedConfig,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    database = resolve_relative_path(
        loaded.config.storage.sqlite_path, loaded.base_dir
    ).resolve()
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            if is_uninitialized_schema(connection):
                return None
            require_current_schema(connection)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"product_policies", "chat_policies"} <= tables:
                return None
            row = connection.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (PRODUCT_POLICY_KEY,),
            ).fetchone()
            if row is None:
                return None
            policy = json.loads(row["policy_json"])
            if not isinstance(policy, dict):
                raise ConfigError("production policy has an invalid format")
            chats = [
                {
                    "chat_id": item["chat_id"],
                    "name": item["name"] or "",
                    "auto_reply": bool(item["auto_reply"]),
                    "bot_joined": bool(item["bot_joined"]),
                    "reply_identity": item["reply_identity"],
                    "allow_user_fallback": bool(item["allow_user_fallback"]),
                    "resource_download": bool(item["resource_download"]),
                }
                for item in connection.execute(
                    "SELECT chat_id, name, auto_reply, bot_joined, reply_identity, allow_user_fallback, resource_download FROM chat_policies ORDER BY chat_id"
                )
            ]
            return cast(dict[str, Any], policy), chats
    except (sqlite3.Error, json.JSONDecodeError, IncompatibleSchemaError) as exc:
        raise ConfigError(f"cannot read production policy for dry-run: {exc}") from exc


def _apply_snapshot(
    store: SQLiteStore,
    global_policy: dict[str, Any],
    chats: list[dict[str, Any]],
    *,
    source: str,
) -> None:
    try:
        store.replace_product_policy_snapshot(global_policy, chats, source=source)
    except (ValueError, sqlite3.Error) as exc:
        raise ConfigError(f"production policy snapshot is invalid: {exc}") from exc
