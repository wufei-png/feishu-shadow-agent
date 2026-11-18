from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from feishu_shadow_agent.coworker_memory import MemoryEntry, SharedMemory


def test_agent_can_append_sourced_memory_through_the_module_cli(tmp_path: Path) -> None:
    path = tmp_path / "shared.md"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "feishu_shadow_agent.coworker_memory",
            "--path",
            str(path),
            "--source",
            "message=om_1",
            "--text",
            "偏好简洁回复",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "message=om_1" in path.read_text(encoding="utf-8")
    assert "偏好简洁回复" in path.read_text(encoding="utf-8")


def test_memory_updates_existing_fact_atomically_and_keeps_other_notes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared.md"
    memory = SharedMemory(path)
    assert (
        memory.append(
            [
                MemoryEntry("周五演示", "message=old"),
                MemoryEntry("项目使用飞书", "message=other"),
            ]
        )
        == 2
    )
    assert (
        memory.append([MemoryEntry("周一演示", "message=new")], replace=["周五演示"])
        == 1
    )
    content = path.read_text(encoding="utf-8")
    assert "周五演示" not in content
    assert "周一演示" in content and "项目使用飞书" in content
    assert memory.append([MemoryEntry("周一演示", "message=new")]) == 0


def test_memory_rejects_replacing_unknown_fact_without_modifying_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared.md"
    memory = SharedMemory(path)
    memory.append([MemoryEntry("原有事实", "message=old")])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="does not match existing"):
        memory.append([MemoryEntry("新事实", "message=new")], replace=["不存在"])
    assert path.read_bytes() == before
