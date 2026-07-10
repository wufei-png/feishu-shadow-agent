from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..config import ConfigService, LoadedConfig
from .artifacts import file_sha256, read_yaml


def load_evaluation_config(path: str | Path | None) -> LoadedConfig:
    loaded = ConfigService().load(path)
    metadata_path = loaded.path.parent / "metadata.yaml"
    if not metadata_path.is_file():
        return loaded
    metadata = read_yaml(metadata_path)
    base_dir = metadata.get("config_base_dir")
    config_hash = metadata.get("config_hash")
    if (
        not isinstance(base_dir, str)
        or not base_dir
        or config_hash != file_sha256(loaded.path)
    ):
        return loaded
    return replace(loaded, base_dir=Path(base_dir).expanduser())
