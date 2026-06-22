from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .types import utc_now_iso


class JSONLLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def emit(
        self,
        level: str,
        event: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": utc_now_iso(),
            "level": level,
            "run_id": run_id,
            "task_id": task_id,
            "event": event,
            "data": data or {},
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=_json_default))
            handle.write("\n")


def _json_default(value: Any) -> str:
    return str(value)
