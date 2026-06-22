from __future__ import annotations

import json
from pathlib import Path

from feishu_shadow_agent.jsonl import JSONLLogger


def test_jsonl_logger_writes_valid_line_and_creates_directory(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "agent.jsonl"
    logger = JSONLLogger(path)

    logger.emit("info", "event_name", run_id="run_1", data={"error": ValueError("bad")})

    line = path.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["level"] == "info"
    assert record["run_id"] == "run_1"
    assert record["task_id"] is None
    assert record["event"] == "event_name"
    assert record["data"]["error"] == "bad"
