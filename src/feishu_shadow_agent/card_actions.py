# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol, cast

from .approval_cards import CARD_ACTION_PROTOCOL
from .config import AppConfig
from .jsonl import JSONLLogger
from .operator_commands import ApprovalCommandService, CommandResult
from .store.sqlite_store import SQLiteStore
from .types import ExecutionMode, FeedbackReason

CardActionName = Literal[
    "send_suggestion",
    "edit_send",
    "no_send_keep_watching",
    "no_send_end_task",
]
ConnectionStatus = Literal["disabled", "starting", "healthy", "unhealthy", "stopped"]
VALID_CARD_ACTIONS = {
    "send_suggestion",
    "edit_send",
    "no_send_keep_watching",
    "no_send_end_task",
}
VALID_FEEDBACK_REASONS = {
    "inaccurate_or_unsupported",
    "incomplete_context",
    "tone_or_style",
    "unnecessary_reply",
    "other",
}


class ChannelClient(Protocol):
    def on(self, name: str, handler: Callable[..., Any]) -> Callable[[], Any]: ...

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None: ...

    async def disconnect(self) -> None: ...


ChannelFactory = Callable[[str, str], ChannelClient]


@dataclass(frozen=True)
class CardActionRequest:
    event_id: str
    operator_open_id: str
    approval_id: str
    action: CardActionName
    final_reply: str | None = None
    feedback_reason: FeedbackReason | None = None
    note: str | None = None


@dataclass(frozen=True)
class CardActionOutcome:
    status: str
    toast_type: Literal["success", "warning", "error"]
    toast: str
    command_result: dict[str, Any] | None = None


class CardActionProcessor:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        config: AppConfig,
        logger: JSONLLogger,
        wake: Callable[[], None],
        execution_mode: ExecutionMode,
    ):
        self.config = config
        self.logger = logger
        self.wake = wake
        self.execution_mode = execution_mode
        self.commands = ApprovalCommandService(
            store,
            keep_watching_until_factory=lambda: (
                datetime.now().astimezone()
                + timedelta(minutes=config.lifecycle.watch_minutes)
            ).isoformat(timespec="seconds"),
        )

    def handle(self, event: Any) -> CardActionOutcome:
        try:
            request = parse_card_action(event)
        except ValueError as exc:
            self.logger.warning(
                "card_action_rejected", data={"reason": str(exc), "stage": "parse"}
            )
            return CardActionOutcome("rejected", "error", str(exc))
        if request.operator_open_id != self.config.owner.open_id:
            self.logger.warning(
                "card_action_rejected",
                data={
                    "reason": "operator is not the configured owner",
                    "event_id": request.event_id,
                    "operator_open_id": request.operator_open_id,
                    "approval_id": request.approval_id,
                },
            )
            return CardActionOutcome(
                "forbidden", "error", "仅配置的 owner 可以操作此卡片。"
            )

        result = self._apply(request)
        outcome = CardActionOutcome(
            status=result.status,
            toast_type="success"
            if result.status in {"applied", "no_change"}
            else "error",
            toast="操作已进入队列。"
            if result.status in {"applied", "no_change"}
            else _command_error(result),
            command_result=result.as_dict(),
        )
        self.logger.emit(
            "info" if outcome.toast_type == "success" else "error",
            "card_action_processed",
            data={
                "event_id": request.event_id,
                "approval_id": request.approval_id,
                "action": request.action,
                "status": result.status,
                "execution_mode": self.execution_mode,
            },
        )
        if outcome.toast_type == "success":
            self.wake()
        return outcome

    def _apply(self, request: CardActionRequest) -> CommandResult:
        common: dict[str, Any] = {
            "actor": request.operator_open_id,
            "command_id": f"card:{request.event_id}",
            "feedback_reason": request.feedback_reason,
            "note": request.note,
            "execution_mode": self.execution_mode,
        }
        if request.action == "send_suggestion":
            return self.commands.approve(request.approval_id, **common)
        if request.action == "edit_send":
            return self.commands.send(
                request.approval_id, request.final_reply or "", **common
            )
        return self.commands.reject(
            request.approval_id,
            keep_watching=request.action == "no_send_keep_watching",
            **common,
        )


@dataclass
class FeishuCardActionConnection:
    processor: CardActionProcessor
    app_id: str
    app_secret: str
    startup_timeout_seconds: int
    logger: JSONLLogger
    channel_factory: ChannelFactory = field(default=lambda a, s: _create_channel(a, s))
    _status: ConnectionStatus = field(default="disabled", init=False)
    _last_error: str | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    _startup_event: threading.Event = field(default_factory=threading.Event, init=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return self.is_healthy()
        self._stop_event.clear()
        self._startup_event.clear()
        self._set_status("starting")
        self._thread = threading.Thread(
            target=self._thread_main,
            name="feishu-card-actions",
            daemon=True,
        )
        self._thread.start()
        self._startup_event.wait(timeout=self.startup_timeout_seconds + 1)
        if not self._startup_event.is_set():
            self._set_status("unhealthy", "card callback connection startup timed out")
        return self.is_healthy()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(self.startup_timeout_seconds, 2) + 1)
        self._set_status("stopped")

    def is_healthy(self) -> bool:
        with self._state_lock:
            return self._status == "healthy"

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {"status": self._status, "last_error": self._last_error}

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self._set_status("unhealthy", str(exc))
            self.logger.emit(
                "error",
                "card_action_connection_failed",
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
        finally:
            self._startup_event.set()

    async def _run(self) -> None:
        if not self.app_id or not self.app_secret:
            raise ValueError(
                "interactive card credential environment variables are unset"
            )
        channel = self.channel_factory(self.app_id, self.app_secret)
        channel.on("cardAction", self.processor.handle)
        channel.on("reconnecting", lambda: self._set_status("unhealthy"))
        channel.on("reconnected", lambda: self._set_status("healthy"))
        channel.on("error", self._on_channel_error)
        try:
            await channel.connect_until_ready(timeout=self.startup_timeout_seconds)
            self._set_status("healthy")
            self._startup_event.set()
            self.logger.emit("info", "card_action_connection_ready")
            while not self._stop_event.is_set():
                await asyncio.sleep(0.1)
        finally:
            await channel.disconnect()

    def _on_channel_error(self, exc: object) -> None:
        self._set_status("unhealthy", str(exc))

    def _set_status(self, status: ConnectionStatus, error: str | None = None) -> None:
        with self._state_lock:
            self._status = status
            if error is not None:
                self._last_error = error
            elif status == "healthy":
                self._last_error = None


def create_card_action_connection(
    *,
    store: SQLiteStore,
    config: AppConfig,
    logger: JSONLLogger,
    wake: Callable[[], None],
    execution_mode: ExecutionMode,
    channel_factory: ChannelFactory | None = None,
) -> FeishuCardActionConnection:
    cards = config.interactive_cards
    processor = CardActionProcessor(
        store=store,
        config=config,
        logger=logger,
        wake=wake,
        execution_mode=execution_mode,
    )
    kwargs: dict[str, Any] = {}
    if channel_factory is not None:
        kwargs["channel_factory"] = channel_factory
    return FeishuCardActionConnection(
        processor=processor,
        app_id=os.environ.get(cards.app_id_env, ""),
        app_secret=os.environ.get(cards.app_secret_env, ""),
        startup_timeout_seconds=cards.startup_timeout_seconds,
        logger=logger,
        **kwargs,
    )


def parse_card_action(event: Any) -> CardActionRequest:
    operator_open_id = _attr(event, "operator", "open_id")
    if not operator_open_id:
        raise ValueError("card action is missing operator.open_id")
    action = getattr(event, "action", None)
    value = getattr(action, "value", None)
    if not isinstance(value, dict):
        raise ValueError("card action value must be an object")
    action_value = cast(dict[str, object], value)
    if action_value.get("protocol") != CARD_ACTION_PROTOCOL:
        raise ValueError("unsupported card action protocol")
    action_name = action_value.get("action")
    if not isinstance(action_name, str) or action_name not in VALID_CARD_ACTIONS:
        raise ValueError("unsupported card action")
    approval_id = action_value.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id.startswith("a_"):
        raise ValueError("card action requires a concrete approval_id")
    form_value = getattr(action, "form_value", None)
    form = cast(dict[str, object], form_value) if isinstance(form_value, dict) else {}
    final_reply = _optional_text(form.get("final_reply"))
    if action_name == "edit_send" and not final_reply:
        raise ValueError("编辑后发送需要填写回复内容")
    feedback_reason_value = _optional_text(form.get("feedback_reason"))
    if feedback_reason_value not in VALID_FEEDBACK_REASONS | {None}:
        raise ValueError("unsupported feedback reason")
    note = _optional_text(form.get("note"))
    if note is not None and len(note) > 500:
        raise ValueError("feedback note must be at most 500 characters")
    event_id = _event_id(event)
    return CardActionRequest(
        event_id=event_id,
        operator_open_id=operator_open_id,
        approval_id=approval_id,
        action=cast(CardActionName, action_name),
        final_reply=final_reply,
        feedback_reason=cast(FeedbackReason | None, feedback_reason_value),
        note=note,
    )


def _event_id(event: Any) -> str:
    raw = getattr(event, "raw", None)
    if isinstance(raw, dict):
        raw_payload = cast(dict[str, object], raw)
        header = raw_payload.get("header")
        if isinstance(header, dict):
            event_id = cast(dict[str, object], header).get("event_id")
            if isinstance(event_id, str) and event_id:
                return event_id
    stable = {
        "message_id": getattr(event, "message_id", None),
        "operator_open_id": _attr(event, "operator", "open_id"),
        "value": getattr(getattr(event, "action", None), "value", None),
        "form_value": getattr(getattr(event, "action", None), "form_value", None),
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str)
    return "derived_" + hashlib.sha256(encoded.encode()).hexdigest()


def _attr(value: Any, first: str, second: str) -> str:
    nested = getattr(value, first, None)
    found = getattr(nested, second, None)
    return found.strip() if isinstance(found, str) else ""


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _command_error(result: CommandResult) -> str:
    error = result.result.get("error")
    return str(error) if error else "操作未能入队，请查看文本命令或控制台。"


def _create_channel(app_id: str, app_secret: str) -> ChannelClient:
    try:
        from lark_channel import FeishuChannel
    except ImportError as exc:
        raise RuntimeError(
            "interactive cards require installation with the 'cards' extra"
        ) from exc
    return cast(
        ChannelClient,
        FeishuChannel(app_id=app_id, app_secret=app_secret, transport="ws"),
    )
