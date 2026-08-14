from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import yaml

SKILL_NAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
EXPLICIT_PATHS_HEADING = "可用的非原生 skills（仅提供路径，尚未加载；需要时先读取对应路径并按其中说明执行）："


def load_agent_skill_names(skill_directories: Sequence[str | Path]) -> list[str]:
    names: list[str] = []
    for value in skill_directories:
        directory = Path(value).expanduser()
        content = (directory / "SKILL.md").read_text(encoding="utf-8")
        name = _frontmatter_name(content)
        if name is None or SKILL_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(
                f"invalid or missing skill name in {directory / 'SKILL.md'}"
            )
        if name not in names:
            names.append(name)
    return names


def configured_agent_skill_names(
    skill_directories: Sequence[str | Path],
) -> list[str]:
    try:
        return load_agent_skill_names(skill_directories)
    except (OSError, ValueError):
        return []


def append_explicit_context_paths(prompt: str, paths: Sequence[str | Path]) -> str:
    if not paths:
        return prompt
    lines = [EXPLICIT_PATHS_HEADING, *(f"- `{path}`" for path in paths)]
    return f"{prompt}\n\n" + "\n".join(lines)


def append_codex_skill_mentions(prompt: str, names: Sequence[str]) -> str:
    if not names:
        return prompt
    for name in names:
        if SKILL_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError("Codex skill name is invalid")
    return f"{prompt}\n\n" + " ".join(f"${name}" for name in names)


def _frontmatter_name(content: str) -> str | None:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return None
    try:
        metadata: Any = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    if not isinstance(metadata, dict):
        return None
    metadata_map = cast(dict[str, Any], metadata)
    name = metadata_map.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None
