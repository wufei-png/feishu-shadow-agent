from __future__ import annotations

import json
import logging as std_logging
import sys
from pathlib import Path
from typing import Any, Literal

from .types import utc_now_iso

LogLevel = Literal["debug", "info", "warning", "error"]
_LEVELS: dict[str, int] = {
    "debug": std_logging.DEBUG,
    "info": std_logging.INFO,
    "warning": std_logging.WARNING,
    "error": std_logging.ERROR,
}


class JSONLLogger:
    def __init__(
        self,
        path: str | Path,
        *,
        level: LogLevel = "debug",
        console: bool = False,
        text_path: str | Path | None = None,
    ):
        self.path = Path(path)
        self.level = level
        self.console = console
        self.text_path = None if text_path is None else Path(text_path)
        self._logger = std_logging.getLogger(f"feishu_shadow_agent.{id(self)}")
        self._logger.handlers = []
        self._logger.propagate = False
        self._logger.setLevel(std_logging.DEBUG)
        self._configure_handlers()

    def emit(
        self,
        level: LogLevel | str,
        event: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        numeric_level = _level_number(level)
        self._logger.log(
            numeric_level,
            event,
            extra={
                "event": event,
                "run_id": run_id,
                "task_id": task_id,
                "data": data or {},
            },
        )

    def debug(
        self,
        event: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.emit("debug", event, run_id=run_id, task_id=task_id, data=data)

    def info(
        self,
        event: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.emit("info", event, run_id=run_id, task_id=task_id, data=data)

    def warning(
        self,
        event: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.emit("warning", event, run_id=run_id, task_id=task_id, data=data)

    def error(
        self,
        event: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.emit("error", event, run_id=run_id, task_id=task_id, data=data)

    def _configure_handlers(self) -> None:
        level_number = _level_number(self.level)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        json_handler = std_logging.FileHandler(self.path, encoding="utf-8")
        json_handler.setLevel(level_number)
        json_handler.setFormatter(_JSONLFormatter())
        self._logger.addHandler(json_handler)

        if self.text_path is not None:
            self.text_path.parent.mkdir(parents=True, exist_ok=True)
            text_handler = std_logging.FileHandler(self.text_path, encoding="utf-8")
            text_handler.setLevel(level_number)
            text_handler.setFormatter(_TextFormatter())
            self._logger.addHandler(text_handler)

        if self.console:
            console_handler = std_logging.StreamHandler(sys.stderr)
            console_handler.setLevel(level_number)
            console_handler.setFormatter(_TextFormatter())
            self._logger.addHandler(console_handler)


class _JSONLFormatter(std_logging.Formatter):
    def format(self, record: std_logging.LogRecord) -> str:
        payload = {
            "ts": utc_now_iso(),
            "level": record.levelname.lower(),
            "run_id": getattr(record, "run_id", None),
            "task_id": getattr(record, "task_id", None),
            "event": getattr(record, "event", record.getMessage()),
            "data": getattr(record, "data", {}) or {},
        }
        return json.dumps(payload, ensure_ascii=False, default=_json_default)


class _TextFormatter(std_logging.Formatter):
    def format(self, record: std_logging.LogRecord) -> str:
        event = getattr(record, "event", record.getMessage())
        parts = [
            utc_now_iso(),
            record.levelname.lower(),
            str(event),
        ]
        run_id = getattr(record, "run_id", None)
        task_id = getattr(record, "task_id", None)
        if run_id:
            parts.append(f"run_id={run_id}")
        if task_id:
            parts.append(f"task_id={task_id}")
        data = getattr(record, "data", {}) or {}
        if isinstance(data, dict):
            parts.extend(f"{key}={_text_value(value)}" for key, value in data.items())
        else:
            parts.append(f"data={_text_value(data)}")
        return " ".join(parts)


def _level_number(level: LogLevel | str) -> int:
    try:
        return _LEVELS[str(level).lower()]
    except KeyError as exc:
        raise ValueError("log level must be one of debug, info, warning, error") from exc


def _text_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text or any(char.isspace() for char in text):
        return repr(text)
    return text


def _json_default(value: Any) -> str:
    return str(value)
