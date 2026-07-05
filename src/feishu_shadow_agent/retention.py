from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import AppConfig
from .jsonl import JSONLLogger
from .store.sqlite_store import SQLiteStore

RAW_JSON_PRUNED_PLACEHOLDER = json.dumps({"retention_pruned": True}, sort_keys=True)
RETENTION_CHECKPOINT_KEY = "retention.last_pruned_at"
RETENTION_DAEMON_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class RetentionSkippedResource:
    resource_id: int
    message_id: str
    path: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "message_id": self.message_id,
            "path": self.path,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RetentionSummary:
    dry_run: bool
    raw_message_cutoff: str
    resource_cutoff: str
    raw_messages_pruned: int = 0
    resources_candidates: int = 0
    resources_deleted: int = 0
    resources_expired: int = 0
    resources_skipped: list[RetentionSkippedResource] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "raw_message_cutoff": self.raw_message_cutoff,
            "resource_cutoff": self.resource_cutoff,
            "raw_messages_pruned": self.raw_messages_pruned,
            "resources_candidates": self.resources_candidates,
            "resources_deleted": self.resources_deleted,
            "resources_expired": self.resources_expired,
            "resources_skipped": [
                resource.as_dict() for resource in self.resources_skipped
            ],
        }


class RetentionService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        config: AppConfig,
        base_dir: str | Path,
        logger: JSONLLogger | None = None,
    ):
        self.store = store
        self.config = config
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.logger = logger

    def prune(
        self,
        *,
        run_id: str | None = None,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> RetentionSummary:
        now = now or datetime.now().astimezone()
        raw_cutoff = _cutoff_iso(now, self.config.retention.raw_message_days)
        resource_cutoff = _cutoff_iso(now, self.config.retention.resource_days)
        raw_count = self.store.count_prunable_message_raw_json(
            cutoff=raw_cutoff,
            replacement_json=RAW_JSON_PRUNED_PLACEHOLDER,
        )
        resource_rows = self.store.list_prunable_resources(cutoff=resource_cutoff)
        skipped: list[RetentionSkippedResource] = []
        deleted_resource_ids: list[int] = []
        expired_resource_ids: list[int] = []

        for row in resource_rows:
            resource_id = int(row["id"])
            stored_path = row["path"]
            resolved = self._resolve_resource_path(stored_path)
            if isinstance(resolved, str):
                if resolved == "missing_file":
                    if not dry_run:
                        expired_resource_ids.append(resource_id)
                    continue
                skipped.append(_skipped(row, resolved))
                continue
            if dry_run:
                continue
            try:
                resolved.unlink()
            except OSError as exc:
                skipped.append(_skipped(row, f"delete_failed: {exc}"))
                continue
            deleted_resource_ids.append(resource_id)
            expired_resource_ids.append(resource_id)

        raw_pruned = raw_count
        expired = len(expired_resource_ids)
        if not dry_run:
            raw_pruned = self.store.prune_message_raw_json(
                cutoff=raw_cutoff,
                replacement_json=RAW_JSON_PRUNED_PLACEHOLDER,
            )
            expired = self.store.mark_resources_expired(expired_resource_ids)

        summary = RetentionSummary(
            dry_run=dry_run,
            raw_message_cutoff=raw_cutoff,
            resource_cutoff=resource_cutoff,
            raw_messages_pruned=raw_pruned,
            resources_candidates=len(resource_rows),
            resources_deleted=0 if dry_run else len(deleted_resource_ids),
            resources_expired=0 if dry_run else expired,
            resources_skipped=skipped,
        )
        if self.logger is not None:
            self.logger.emit(
                "info", "retention_pruned", run_id=run_id, data=summary.as_dict()
            )
        return summary

    def _resolve_resource_path(self, stored_path: Any) -> Path | str:
        if not isinstance(stored_path, str) or not stored_path:
            return "missing_path"
        relative_path = Path(stored_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return "unsafe_path"
        resource_root = (self.base_dir / self.config.storage.resource_dir).resolve(
            strict=False
        )
        candidate = self.base_dir / relative_path
        resolved_candidate = candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resource_root)
        except ValueError:
            return "outside_resource_dir"
        if candidate.is_symlink():
            return "not_regular_file"
        if not candidate.exists():
            return "missing_file"
        if not candidate.is_file():
            return "not_regular_file"
        return candidate


def daemon_retention_is_due(store: SQLiteStore, *, now: datetime | None = None) -> bool:
    checkpoint = store.get_checkpoint(RETENTION_CHECKPOINT_KEY)
    if not checkpoint or not isinstance(checkpoint.get("last_pruned_at"), str):
        return True
    now = now or datetime.now().astimezone()
    try:
        last_pruned = datetime.fromisoformat(checkpoint["last_pruned_at"])
    except ValueError:
        return True
    if last_pruned.tzinfo is None:
        last_pruned = last_pruned.astimezone()
    return now - last_pruned >= timedelta(seconds=RETENTION_DAEMON_INTERVAL_SECONDS)


def record_daemon_retention_checkpoint(
    store: SQLiteStore,
    *,
    summary: RetentionSummary,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now().astimezone()
    store.set_checkpoint(
        RETENTION_CHECKPOINT_KEY,
        {
            "last_pruned_at": now.isoformat(timespec="seconds"),
            "summary": summary.as_dict(),
        },
    )


def _cutoff_iso(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat(timespec="seconds")


def _skipped(row: Any, reason: str) -> RetentionSkippedResource:
    return RetentionSkippedResource(
        resource_id=int(row["id"]),
        message_id=str(row["message_id"]),
        path=row["path"],
        reason=reason,
    )
