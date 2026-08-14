from __future__ import annotations

import fcntl
import json
import logging as std_logging
import os
import sys
import threading
from collections.abc import Collection, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .time_utils import parse_instant
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
        if self.text_path is not None and _same_file_path(self.path, self.text_path):
            raise ValueError("JSONL and text logs must use different files")
        self._handler_lock = threading.RLock()
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
        with self._handler_lock:
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
        json_handler = _CoordinatedFileHandler(self.path, encoding="utf-8")
        self.path.chmod(0o600)
        json_handler.setLevel(level_number)
        json_handler.setFormatter(_JSONLFormatter())
        self._logger.addHandler(json_handler)

        if self.text_path is not None:
            self.text_path.parent.mkdir(parents=True, exist_ok=True)
            text_handler = _CoordinatedFileHandler(self.text_path, encoding="utf-8")
            self.text_path.chmod(0o600)
            text_handler.setLevel(level_number)
            text_handler.setFormatter(_TextFormatter())
            self._logger.addHandler(text_handler)

        if self.console:
            console_handler = std_logging.StreamHandler(sys.stderr)
            console_handler.setLevel(level_number)
            console_handler.setFormatter(_TextFormatter())
            self._logger.addHandler(console_handler)

    def scrub_before(
        self,
        cutoff: str,
        *,
        dry_run: bool,
        protected_task_ids: Collection[str] = (),
        protected_message_ids: Collection[str] = (),
        protected_action_ids: Collection[str] = (),
        protected_approval_ids: Collection[str] = (),
    ) -> dict[str, int]:
        """Remove old log payloads while preserving minimal event metadata."""
        with self._handler_lock:
            return self._scrub_before_locked(
                cutoff,
                dry_run=dry_run,
                protected_task_ids=frozenset(protected_task_ids),
                protected_message_ids=frozenset(protected_message_ids),
                protected_action_ids=frozenset(protected_action_ids),
                protected_approval_ids=frozenset(protected_approval_ids),
            )

    def _scrub_before_locked(
        self,
        cutoff: str,
        *,
        dry_run: bool,
        protected_task_ids: frozenset[str],
        protected_message_ids: frozenset[str],
        protected_action_ids: frozenset[str],
        protected_approval_ids: frozenset[str],
    ) -> dict[str, int]:
        paths = [("jsonl", self.path, True)]
        if self.text_path is not None:
            paths.append(("text", self.text_path, False))
        with ExitStack() as locks:
            for path in sorted({path for _, path, _ in paths}):
                locks.enter_context(_exclusive_log_lock(path))
            for handler in self._logger.handlers:
                handler.flush()
            if dry_run:
                return {
                    name: _scrub_log_file(
                        path,
                        cutoff=cutoff,
                        jsonl=jsonl,
                        dry_run=True,
                        protected_task_ids=protected_task_ids,
                        protected_message_ids=protected_message_ids,
                        protected_action_ids=protected_action_ids,
                        protected_approval_ids=protected_approval_ids,
                    )
                    for name, path, jsonl in paths
                }

            handlers = list(self._logger.handlers)
            for handler in handlers:
                self._logger.removeHandler(handler)
                handler.close()
            try:
                return {
                    name: _scrub_log_file(
                        path,
                        cutoff=cutoff,
                        jsonl=jsonl,
                        dry_run=False,
                        protected_task_ids=protected_task_ids,
                        protected_message_ids=protected_message_ids,
                        protected_action_ids=protected_action_ids,
                        protected_approval_ids=protected_approval_ids,
                    )
                    for name, path, jsonl in paths
                }
            finally:
                self._configure_handlers()


class _CoordinatedFileHandler(std_logging.FileHandler):
    """Serialize writers with retention and reopen after atomic replacement."""

    def __init__(self, filename: str | Path, *, encoding: str):
        self.coordinated_path = Path(filename)
        super().__init__(filename, encoding=encoding)

    def emit(self, record: std_logging.LogRecord) -> None:
        with _exclusive_log_lock(self.coordinated_path):
            self._reopen_if_replaced()
            super().emit(record)

    def _reopen_if_replaced(self) -> None:
        if self.stream is None:
            self.stream = self._open()
            return
        try:
            current = self.coordinated_path.stat()
            opened = os.fstat(self.stream.fileno())
        except (FileNotFoundError, OSError):
            current = None
            opened = None
        if (
            current is not None
            and opened is not None
            and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
        ):
            return
        self.stream.flush()
        self.stream.close()
        self.stream = self._open()


@contextmanager
def _exclusive_log_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


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
        raise ValueError(
            "log level must be one of debug, info, warning, error"
        ) from exc


def _same_file_path(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return False


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


def _scrub_log_file(
    path: Path,
    *,
    cutoff: str,
    jsonl: bool,
    dry_run: bool,
    protected_task_ids: frozenset[str],
    protected_message_ids: frozenset[str],
    protected_action_ids: frozenset[str],
    protected_approval_ids: frozenset[str],
) -> int:
    if not path.exists():
        return 0
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"log retention requires a regular file: {path}")
    cutoff_instant = parse_instant(cutoff)
    temporary_path = path.with_name(f".{path.name}.retention-{uuid4().hex}.tmp")
    changed = 0
    output = None if dry_run else temporary_path.open("x", encoding="utf-8")
    try:
        if output is not None:
            temporary_path.chmod(0o600)
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                replacement, should_scrub = (
                    _scrub_jsonl_line(
                        line,
                        cutoff_instant,
                        protected_task_ids,
                        protected_message_ids,
                        protected_action_ids,
                        protected_approval_ids,
                    )
                    if jsonl
                    else _scrub_text_line(
                        line,
                        cutoff_instant,
                        protected_task_ids,
                        protected_message_ids,
                        protected_action_ids,
                        protected_approval_ids,
                    )
                )
                if should_scrub:
                    changed += 1
                if output is not None:
                    output.write(replacement)
        if output is None:
            return changed
        output.flush()
        os.fsync(output.fileno())
        output.close()
        output = None
        if changed:
            os.replace(temporary_path, path)
            _fsync_directory(path.parent)
        else:
            temporary_path.unlink()
        return changed
    finally:
        if output is not None:
            output.close()
        temporary_path.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        os.close(directory_fd)


def _scrub_jsonl_line(
    line: str,
    cutoff: datetime,
    protected_task_ids: frozenset[str],
    protected_message_ids: frozenset[str],
    protected_action_ids: frozenset[str],
    protected_approval_ids: frozenset[str],
) -> tuple[str, bool]:
    try:
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError("log line is not an object")
        if payload.get("event") == "retention_unparseable_line_pruned" and payload.get(
            "data"
        ) == {"retention_pruned": True}:
            return line, False
        timestamp = parse_instant(str(payload.get("ts", "")))
    except (json.JSONDecodeError, ValueError):
        replacement = {
            "ts": None,
            "level": "warning",
            "run_id": None,
            "task_id": None,
            "event": "retention_unparseable_line_pruned",
            "data": {"retention_pruned": True},
        }
        return json.dumps(replacement, ensure_ascii=False) + "\n", True
    if timestamp > cutoff:
        return line, False
    task_id = payload.get("task_id")
    if task_id is not None and str(task_id) in protected_task_ids:
        return line, False
    data = payload.get("data")
    if (
        isinstance(data, dict)
        and data.get("message_id") is not None
        and str(data["message_id"]) in protected_message_ids
    ):
        return line, False
    if isinstance(data, dict) and any(
        data.get(key) is not None and str(data[key]) in protected
        for key, protected in (
            ("action_id", protected_action_ids),
            ("approval_id", protected_approval_ids),
        )
    ):
        return line, False
    minimal = {
        key: payload.get(key) for key in ("ts", "level", "run_id", "task_id", "event")
    }
    minimal["data"] = {"retention_pruned": True}
    replacement = json.dumps(minimal, ensure_ascii=False, default=_json_default) + "\n"
    if payload == minimal:
        return line, False
    return replacement, True


def _scrub_text_line(
    line: str,
    cutoff: datetime,
    protected_task_ids: frozenset[str],
    protected_message_ids: frozenset[str],
    protected_action_ids: frozenset[str],
    protected_approval_ids: frozenset[str],
) -> tuple[str, bool]:
    if line == "retention_unparseable_line_pruned retention_pruned=true\n":
        return line, False
    parts = line.rstrip("\n").split(maxsplit=3)
    try:
        timestamp = parse_instant(parts[0])
    except (IndexError, ValueError):
        return "retention_unparseable_line_pruned retention_pruned=true\n", True
    if timestamp > cutoff:
        return line, False
    if any(
        part.removeprefix("task_id=") in protected_task_ids
        for part in line.rstrip("\n").split()
        if part.startswith("task_id=")
    ):
        return line, False
    if any(
        part.removeprefix(f"{key}=") in protected
        for part in line.rstrip("\n").split()
        for key, protected in (
            ("action_id", protected_action_ids),
            ("approval_id", protected_approval_ids),
        )
        if part.startswith(f"{key}=")
    ):
        return line, False
    if any(
        part.removeprefix("message_id=") in protected_message_ids
        for part in line.rstrip("\n").split()
        if part.startswith("message_id=")
    ):
        return line, False
    minimal = " ".join(parts[:3]) + " retention_pruned=true\n"
    if line == minimal:
        return line, False
    return minimal, True
