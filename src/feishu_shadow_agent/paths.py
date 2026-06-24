from __future__ import annotations

from pathlib import Path


def resolve_relative_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return base_dir / path


def resolve_agent_skill_path(value: str | Path, base_dir: Path) -> Path:
    path = resolve_relative_path(value, base_dir)
    if path.name == "SKILL.md":
        return path.parent
    return path
