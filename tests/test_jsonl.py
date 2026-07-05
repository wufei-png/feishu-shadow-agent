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


def test_jsonl_logger_level_helpers(tmp_path: Path) -> None:
    path = tmp_path / "agent.jsonl"
    logger = JSONLLogger(path)

    logger.debug("debug_event")
    logger.info("info_event")
    logger.warning("warning_event")
    logger.error("error_event")

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["level"] for record in records] == [
        "debug",
        "info",
        "warning",
        "error",
    ]
    assert [record["event"] for record in records] == [
        "debug_event",
        "info_event",
        "warning_event",
        "error_event",
    ]


def test_jsonl_logger_filters_by_level(tmp_path: Path) -> None:
    path = tmp_path / "agent.jsonl"
    logger = JSONLLogger(path, level="warning")

    logger.debug("debug_event")
    logger.info("info_event")
    logger.warning("warning_event")
    logger.error("error_event")

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event"] for record in records] == ["warning_event", "error_event"]


def test_jsonl_logger_can_emit_human_readable_text_log(
    tmp_path: Path,
    capsys,
) -> None:
    jsonl_path = tmp_path / "agent.jsonl"
    text_path = tmp_path / "agent.log"
    logger = JSONLLogger(jsonl_path, console=True, text_path=text_path)

    logger.error(
        "event_name",
        run_id="run_1",
        task_id="task_1",
        data={"stage": "dispatch", "error": "bad thing"},
    )

    console_output = capsys.readouterr().err
    text_output = text_path.read_text(encoding="utf-8")
    for output in (console_output, text_output):
        assert "error event_name" in output
        assert "run_id=run_1" in output
        assert "task_id=task_1" in output
        assert "stage=dispatch" in output
        assert "bad thing" in output

    record = json.loads(jsonl_path.read_text(encoding="utf-8"))
    assert record["level"] == "error"
    assert record["event"] == "event_name"
