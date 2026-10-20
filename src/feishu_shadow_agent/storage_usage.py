from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .paths import resolve_relative_path

DiskUsageProbe = Callable[[Path], tuple[int, int, int]]


@dataclass(frozen=True)
class StorageUsage:
    """Read-only view of the runtime storage footprint.

    Byte counts are measured with stat/walk only; this never creates, truncates,
    or rewrites a file. A missing sqlite or log file reports 0, and a text log
    that is not configured reports None.
    """

    sqlite_bytes: int
    jsonl_log_bytes: int
    text_log_bytes: int | None
    resource_dir_bytes: int
    resource_dir_quota_bytes: int
    filesystem_path: str
    filesystem_total_bytes: int
    filesystem_free_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "sqlite_bytes": self.sqlite_bytes,
            "jsonl_log_bytes": self.jsonl_log_bytes,
            "text_log_bytes": self.text_log_bytes,
            "resource_dir_bytes": self.resource_dir_bytes,
            "resource_dir_quota_bytes": self.resource_dir_quota_bytes,
            "filesystem_path": self.filesystem_path,
            "filesystem_total_bytes": self.filesystem_total_bytes,
            "filesystem_free_bytes": self.filesystem_free_bytes,
        }


def directory_bytes(root: Path) -> int:
    """Sum regular-file sizes under root, ignoring symlinks and unreadable paths."""
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            continue
    return total


def file_bytes(path: Path) -> int:
    """Size of one configured file; a symlinked path still describes its data.

    Directory walks skip links to avoid double counting, but a single configured
    sqlite or log path is the operator's chosen file, so follow the link.
    """
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def resolve_storage_usage(
    config: AppConfig,
    base_dir: str | Path,
    *,
    disk_usage: DiskUsageProbe = shutil.disk_usage,
) -> StorageUsage:
    """Measure runtime storage and report the tightest runtime filesystem.

    The sqlite, log, and resource roots can live on different mounts, so the
    reported filesystem is the one with the least free space among the runtime
    roots that exist. Roots sharing one volume are measured independently; they
    report the same free space, and the tie-break keeps the result stable.
    """
    base = Path(base_dir).expanduser().resolve()
    sqlite_path = resolve_relative_path(config.storage.sqlite_path, base)
    jsonl_path = resolve_relative_path(config.logging.jsonl_path, base)
    text_path = (
        None
        if config.logging.text_path is None
        else resolve_relative_path(config.logging.text_path, base)
    )
    resource_dir = resolve_relative_path(config.storage.resource_dir, base)

    root_candidates = [sqlite_path.parent, jsonl_path.parent, resource_dir]
    if text_path is not None:
        root_candidates.append(text_path.parent)
    volumes: dict[Path, tuple[int, int, int]] = {}
    for candidate in root_candidates:
        existing = _nearest_existing_directory(candidate)
        if existing is None:
            continue
        resolved = existing.resolve()
        if resolved not in volumes:
            volumes[resolved] = disk_usage(resolved)
    if not volumes:
        raise RuntimeError(f"no existing storage root under {base}")

    filesystem_path, (total_bytes, _, free_bytes) = min(
        volumes.items(), key=lambda item: (item[1][2], str(item[0]))
    )
    return StorageUsage(
        sqlite_bytes=file_bytes(sqlite_path),
        jsonl_log_bytes=file_bytes(jsonl_path),
        text_log_bytes=None if text_path is None else file_bytes(text_path),
        resource_dir_bytes=directory_bytes(resource_dir),
        resource_dir_quota_bytes=config.storage.max_resource_dir_bytes,
        filesystem_path=str(filesystem_path),
        filesystem_total_bytes=total_bytes,
        filesystem_free_bytes=free_bytes,
    )


def _nearest_existing_directory(path: Path) -> Path | None:
    candidate = path.expanduser()
    while True:
        if candidate.is_dir():
            return candidate
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent


__all__ = [
    "DiskUsageProbe",
    "StorageUsage",
    "directory_bytes",
    "file_bytes",
    "resolve_storage_usage",
]
