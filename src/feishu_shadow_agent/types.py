from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

HealthSeverity = Literal["critical", "warning"]
HealthStatus = Literal["ok", "warning", "failed"]


def utc_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


@dataclass(frozen=True)
class HealthCheckResult:
    name: str
    severity: HealthSeverity
    status: HealthStatus
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_critical_failure(self) -> bool:
        return self.severity == "critical" and self.status == "failed"


@dataclass(frozen=True)
class LarkCliResult:
    argv: list[str]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    json_data: Any | None = None
    error: str | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.error is None and not self.timed_out
