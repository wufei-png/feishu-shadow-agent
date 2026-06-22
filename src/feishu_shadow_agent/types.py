from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

ChatType = Literal["group", "p2p"]
HealthSeverity = Literal["critical", "warning"]
HealthStatus = Literal["ok", "warning", "failed"]
RouteName = Literal[
    "new_task",
    "attach_task",
    "reopen_task",
    "close_task",
    "ignore",
    "ambiguous",
    "human_taken_over",
]
SenderRole = Literal["external_user_message", "owner_message", "bot_message", "agent_message"]
TaskStatus = Literal["watching", "waiting_approval", "closed", "closed_by_owner", "human_taken_over"]


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


@dataclass(frozen=True)
class HermesCliResult:
    argv: list[str]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    json_data: Any | None = None
    session_id: str | None = None
    error: str | None = None
    timed_out: bool = False
    latency_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.error is None and not self.timed_out


@dataclass(frozen=True)
class MessagePage:
    items: list[dict[str, Any]]
    next_page_token: str | None = None
    has_more: bool = False
    raw: Any | None = None


@dataclass(frozen=True)
class ResourceRef:
    message_id: str
    file_key: str
    resource_type: Literal["image", "file"]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedMessage:
    message_id: str
    chat_id: str | None
    chat_type: ChatType | None
    sender_id: str | None
    sender_name: str | None
    sender_type: str | None
    sender_role: SenderRole
    sent_at: str | None
    thread_id: str | None
    reply_to_message_id: str | None
    text: str
    direct_mention: bool
    at_all: bool
    mentions: list[str] = field(default_factory=list)
    resources: list[ResourceRef] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_self_message(self) -> bool:
        return self.sender_role in {"bot_message", "agent_message"}


@dataclass(frozen=True)
class TaskRecord:
    id: int
    short_id: str
    status: str
    chat_id: str | None
    chat_type: str | None
    thread_id: str | None
    root_message_id: str | None
    task_label: str | None
    watch_until: str | None
    hermes_session_id: str | None = None


@dataclass(frozen=True)
class ActionRecord:
    id: int
    idempotency_key: str
    task_id: int | None
    approval_id: int | None
    kind: str
    status: str
    target_message_id: str | None
    dry_run: bool
    payload: dict[str, Any]
    result: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskCandidate:
    task: TaskRecord
    matched_by: str


@dataclass(frozen=True)
class RouteDecision:
    route: RouteName
    target_task_id: int | None = None
    target_task_short_id: str | None = None
    reason: str = ""
    candidates_count: int = 0
    shortcut_hit: bool = False
    router_called: bool = False
    matched_by: str | None = None
