from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .time_utils import utc_now_iso

MAX_MEMORY_BYTES = 1024 * 1024
MAX_MEMORY_READ_BYTES = 32000


@dataclass(frozen=True)
class MemoryEntry:
    text: str
    source: str


class SharedMemory:
    """One editable shared file, independent of any particular group."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> str:
        if self.path.is_symlink():
            raise ValueError("shared memory path must not be a symbolic link")
        if not self.path.exists():
            return ""
        content = self.path.read_bytes()
        if len(content) > MAX_MEMORY_BYTES:
            raise ValueError("shared memory exceeds the size limit")
        return content[-MAX_MEMORY_READ_BYTES:].decode("utf-8", errors="replace")

    def append(
        self, entries: list[MemoryEntry], *, replace: list[str] | None = None
    ) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            self.read()
            # Preserve the full file when its display window is smaller.
            full = (
                self.path.read_text(encoding="utf-8")
                if self.path.exists()
                else "# AI 同事共享记忆\n\n"
            )
            replacements = set(replace or [])
            found: set[str] = set()
            if replacements:
                preserved: list[str] = []
                for line in full.splitlines(keepends=True):
                    parts = line.rstrip("\n").split(" | ", 2)
                    try:
                        value = (
                            json.loads(parts[2])
                            if len(parts) == 3 and line.startswith("- ")
                            else None
                        )
                    except json.JSONDecodeError:
                        value = None
                    if isinstance(value, str) and value in replacements:
                        found.add(value)
                    else:
                        preserved.append(line)
                if found != replacements:
                    raise ValueError(
                        "memory replacement does not match existing entries"
                    )
                full = "".join(preserved)
            additions: list[str] = []
            for entry in entries:
                text = " ".join(entry.text.split())
                source = " ".join(entry.source.split())
                if not text or not source or len(text) > 2000 or len(source) > 500:
                    raise ValueError("memory entry has invalid text or provenance")
                token = f"| 来源: {source} | {json.dumps(text, ensure_ascii=False)}\n"
                if token in full or any(token in item for item in additions):
                    continue
                additions.append(f"- {utc_now_iso()} {token}")
            if not additions and not replacements:
                return 0
            updated = full + "".join(additions)
            if len(updated.encode("utf-8")) > MAX_MEMORY_BYTES:
                raise ValueError("shared memory would exceed the size limit")
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(updated)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return len(additions)

    @contextmanager
    def _lock(self) -> Generator[None, None, None]:
        lock_path = self.path.with_name(self.path.name + ".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def maintenance_lock(self) -> Generator[bool, None, None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".maintenance.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description="Append a sourced coworker memory")
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--text", required=True)
    args = parser.parse_args()
    SharedMemory(args.path).append([MemoryEntry(args.text, args.source)])
    print("memory saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
