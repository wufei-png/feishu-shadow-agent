from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..storage_usage import DiskUsageProbe, resolve_storage_usage


class StorageQuery:
    """Read-only storage read model for status and the local console."""

    def __init__(
        self,
        *,
        config: AppConfig | None,
        base_dir: Path | None,
        disk_usage: DiskUsageProbe | None = None,
    ):
        self.config = config
        self.base_dir = base_dir
        self._disk_usage = disk_usage

    def storage_summary(self) -> dict[str, Any] | None:
        if self.config is None or self.base_dir is None:
            return None
        try:
            usage = resolve_storage_usage(
                self.config,
                self.base_dir,
                disk_usage=self._disk_usage or shutil.disk_usage,
            )
        except (OSError, RuntimeError):
            # Storage visibility must never fail the whole dashboard.
            return None
        return usage.as_dict()


__all__ = ["StorageQuery"]
