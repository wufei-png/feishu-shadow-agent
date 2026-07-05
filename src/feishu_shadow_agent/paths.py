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


def resolve_agent_working_dir(value: str | Path | None, base_dir: Path) -> Path:
    if value is None:
        return base_dir.resolve(strict=False)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)
