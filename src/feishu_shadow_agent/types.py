from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from .time_utils import utc_now_iso as _utc_now_iso

ChatType = Literal["group", "p2p"]
HealthSeverity = Literal["critical", "warning"]
HealthStatus = Literal["ok", "warning", "failed"]
SenderRole = Literal[
    "external_user_message", "owner_message", "bot_message", "agent_message"
]
ExecutionMode: TypeAlias = Literal["dry_run", "production"]
ApprovalOutcome: TypeAlias = Literal[
    "suggestion_sent",
    "edited_sent",
    "no_send_keep_watching",
    "no_send_end_task",
]
FeedbackReason: TypeAlias = Literal[
    "inaccurate_or_unsupported",
    "incomplete_context",
    "tone_or_style",
    "unnecessary_reply",
    "other",
]


class TaskStatus(StrEnum):
    WATCHING = "watching"
    CLOSED = "closed"
    CLOSED_BY_OWNER = "closed_by_owner"
    HUMAN_TAKEN_OVER = "human_taken_over"


class ApprovalKind(StrEnum):
    SEND_REPLY = "send_reply"
    TOOL_ACTION = "tool_action"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ActionKind(StrEnum):
    SEND_REPLY = "send_reply"
    OWNER_NOTIFICATION = "owner_notification"


class ActionStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    FAILED_NEEDS_REVIEW = "failed_needs_review"
    CANCELLED = "cancelled"


class DispatchAttemptStatus(StrEnum):
    STARTED = "started"
    DRY_RUN_OK = "dry_run_ok"
    SEND_OK = "send_ok"
    READBACK_OK = "readback_ok"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class DispatchErrorStage(StrEnum):
    CLAIM = "claim"
    DRY_RUN = "dry_run"
    SEND = "send"
    READBACK = "readback"
    RECOVERY = "recovery"


class RunTickStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"


class RouteName(StrEnum):
    NEW_TASK = "new_task"
    ATTACH_TASK = "attach_task"
    REOPEN_TASK = "reopen_task"
    CLOSE_TASK = "close_task"
    IGNORE = "ignore"
    AMBIGUOUS = "ambiguous"
    HUMAN_TAKEN_OVER = "human_taken_over"


class MessageProcessingStage(StrEnum):
    TASK_ROUTER = "task_router"
    TASK_SESSION = "task_session"
    RESOURCE_DOWNLOAD = "resource_download"


class MessageProcessingStatus(StrEnum):
    PROCESSED = "processed"
    PROCESSING_FAILED_TERMINAL = "processing_failed_terminal"
    BLOCKED_WAITING_EXTERNAL = "blocked_waiting_external"


class ResourceStatus(StrEnum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    BOT_NOT_JOINED = "bot_not_joined"
    BOT_INVISIBLE = "bot_invisible"
    FAILED = "failed"
    MISSING_FILE = "missing_file"
    TOO_LARGE = "too_large"
    QUOTA_EXCEEDED = "quota_exceeded"
    EXPIRED = "expired"


CHAT_TYPES = ("group", "p2p")
SENDER_ROLES = (
    "external_user_message",
    "owner_message",
    "bot_message",
    "agent_message",
)


def enum_values(enum_type: type[StrEnum]) -> tuple[str, ...]:
    return tuple(member.value for member in enum_type)


class LifecycleStatePolicy:
    @staticmethod
    def is_active_task_status(status: str) -> bool:
        return status == TaskStatus.WATCHING.value

    @staticmethod
    def task_status_closes_at(status: str) -> bool:
        return status != TaskStatus.WATCHING.value

    @staticmethod
    def message_processing_blocks_duplicate(status: str) -> bool:
        return status in {
            MessageProcessingStatus.PROCESSED.value,
            MessageProcessingStatus.PROCESSING_FAILED_TERMINAL.value,
            MessageProcessingStatus.BLOCKED_WAITING_EXTERNAL.value,
        }

    @staticmethod
    def resource_blocker_status(reason: str) -> str:
        if reason in {
            "resource_needs_bot",
            "resource_download_disabled",
            "resource_too_large",
            "resource_quota_exceeded",
        }:
            return MessageProcessingStatus.BLOCKED_WAITING_EXTERNAL.value
        return MessageProcessingStatus.PROCESSING_FAILED_TERMINAL.value


class StateSchemaContract:
    task_statuses = enum_values(TaskStatus)
    approval_kinds = enum_values(ApprovalKind)
    approval_statuses = enum_values(ApprovalStatus)
    action_kinds = enum_values(ActionKind)
    action_statuses = enum_values(ActionStatus)
    dispatch_attempt_statuses = enum_values(DispatchAttemptStatus)
    dispatch_error_stages = enum_values(DispatchErrorStage)
    run_tick_statuses = enum_values(RunTickStatus)
    route_names = enum_values(RouteName)
    message_processing_stages = enum_values(MessageProcessingStage)
    message_processing_statuses = enum_values(MessageProcessingStatus)
    resource_statuses = enum_values(ResourceStatus)
    chat_types = CHAT_TYPES
    sender_roles = SENDER_ROLES


def utc_now_iso() -> str:
    return _utc_now_iso()


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


@dataclass(frozen=True)
class HealthCheckResult:
    name: str
    severity: HealthSeverity
    status: HealthStatus
    message: str = ""
    details: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

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
    raw: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


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
    mentions: list[str] = field(default_factory=lambda: list[str]())
    resources: list[ResourceRef] = field(default_factory=lambda: list[ResourceRef]())
    raw: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

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
    agent_session_id: str | None = None
    agent_session_provider: str | None = None
    agent_working_dir: str | None = None


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
    execution_mode: ExecutionMode
    payload: dict[str, Any]
    result: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DispatchAttemptRecord:
    id: int
    action_id: int
    run_id: str | None
    claim_token: str
    status: str
    dry_run_result: dict[str, Any] | None
    send_result: dict[str, Any] | None
    readback_result: dict[str, Any] | None
    sent_message_id: str | None
    error_stage: str | None
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class DispatchClaim:
    action: ActionRecord
    attempt: DispatchAttemptRecord


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
