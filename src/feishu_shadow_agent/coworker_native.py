from __future__ import annotations

import json
import sqlite3
import time
from _thread import RLock
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from threading import Condition
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from .agent_backend import AgentRunResult
from .agent_backend_factory import coworker_runtime_config
from .codex_app_server import AppServerError, CodexAppServer, TurnNotActive
from .config import CoworkerConfig
from .coworker import (
    CoworkerBackend,
    CoworkerService,
    CoworkerWorker,
    _code_block,  # pyright: ignore[reportPrivateUsage]
    _mapping,  # pyright: ignore[reportPrivateUsage]
    _prompt_message,  # pyright: ignore[reportPrivateUsage]
    _sender_id,  # pyright: ignore[reportPrivateUsage]
    _SourceUnavailable,  # pyright: ignore[reportPrivateUsage]
    _StartupPolicyChanged,  # pyright: ignore[reportPrivateUsage]
    event_message_from_raw,
)
from .coworker_native_state import NativeCoworkerState
from .coworker_native_store import (
    NativeDelivery,
    NativeDeliveryStore,
    snapshot_evidence,
)
from .coworker_store import (
    GROUP_MAIN_KEY,
    NATIVE_LOCATING_KEY,
    SUBAGENT_EVENT_PREFIX,
    CoworkerEvent,
)
from .dispatcher import _extract_message_id  # pyright: ignore[reportPrivateUsage]
from .message_normalization import (
    mention_identifiers,
    message_content_fingerprint,
    message_snapshot_matches,
)
from .operator_commands import ApprovalCommandService
from .paths import resolve_agent_working_dir, resolve_relative_path
from .policy import PolicyResolver
from .processing import ApprovalService, SendComposer
from .reply_destination import candidate_from_message, message_location
from .time_utils import format_instant, parse_instant_or_none, shift_instant, utc_now
from .types import LarkCliResult, MessagePage, NormalizedMessage

PRIVATE_TOPIC_PREFIX = "__private__:"
_DELIVERY_GUIDANCE = "飞书消息由项目统一发布，禁止用工具发送或回复飞书消息。"
_DRY_RUN_GUIDANCE = "本轮为只读预演。根据已提供的资料起草答复，实际操作留待正式运行。"
_REPLY_GUIDANCE = (
    "返回 action=reply 并填写正文，或返回 action=silent 和空正文。"
    "需要 owner 判断、授权或替其承诺时，设置 approval_required=true 并写明 approval_reason。"
)
_CLARIFICATION_GUIDANCE = (
    "缺少会影响结果的信息时，集中提出必要的澄清问题。"
    "能独立推进的部分先做，并说明采用的假设。"
)
_EVIDENCE_GUIDANCE = (
    "以当前消息和引用为准；旧话题、同名缩写和以前的回答只作线索。含义不明且影响结论时先查证。"
    "需要查项目文档或代码时，优先使用用户指定的仓库；归属不明确时按需使用 docmate 定位。"
    "问题涉及多个相关组件时分别查证；涉及实现、指标、默认值或配置优先级时，核对定义及使用路径。"
    "无法确定原消息或查询失败时如实说明，不要猜测；缺少用户授权时提示 owner 完成授权，不反复发起登录。"
)
_PRIVATE_HISTORY_GUIDANCE = (
    "owner 要求处理转发或引用的工作对话时，以当前请求和引用为准，先用 lark-im 和 "
    "lark-cli --as user 根据原文或其他消息线索检索用户可见的历史和上下文；"
    "确定来源后再缩小范围，不默认限定当前私聊或 owner 为发送者。"
    "用户只要求翻译、改写或整理已有文字时，可直接处理。"
)
_RECENT_SUPPLEMENTS = 4


def _prompt_quote(message: NormalizedMessage) -> dict[str, Any]:
    # The Feishu body keeps inline attachments scoped to the quoted message.
    content = message.raw.get("content")
    quote: dict[str, Any] = {
        "message_id": message.message_id,
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "sender_type": message.sender_type,
        "sent_at": message.sent_at,
        "message_type": message.message_type,
        "content": content if isinstance(content, (str, dict, list)) else message.text,
    }
    for key in ("mentions", "mention_details"):
        if message.raw.get(key):
            quote[key] = message.raw[key]
    return quote


def _is_supplement(text: str) -> bool:
    return (
        text.lstrip()
        .lower()
        .startswith(("补充", "更正", "追加", "supplement:", "correction:"))
    )


class _WatchDeferred(Exception):
    pass


class _ReceiptPending(Exception):
    def __init__(self, message: str, *, private_topics: tuple[str, ...] | None = None):
        super().__init__(message)
        self.private_topics = private_topics


class _WatchExpired(ValueError):
    pass


class _ReplyClient(Protocol):
    def reply_message(
        self,
        *,
        as_identity: str,
        message_id: str,
        text: str,
        idempotency_key: str,
        reply_in_thread: bool = False,
        dry_run: bool = True,
    ) -> LarkCliResult: ...


class _NativeReplyFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    approval_required: bool = False
    approval_reason: str | None = None


class NativeReplyOutput(_NativeReplyFields):
    action: Literal["reply", "silent"]


class NativeMainOutput(_NativeReplyFields):
    action: Literal["reply", "silent", "topic", "transfer"]


class NativeWatchOutput(_NativeReplyFields):
    action: Literal["reply", "silent", "watch", "unwatch"]
    watch_duration_seconds: int | None = None


class SteerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["current", "unrelated", "uncertain"]


@dataclass
class _RootTurn:
    origin: tuple[int, int, str]
    pending_items: set[str] = field(default_factory=set[str])
    completed_items: set[str] = field(default_factory=set[str])
    finished: bool = False


class NativeCoworkerService(CoworkerService):
    """Codex execution with project-owned, receipt-backed replies."""

    def __init__(
        self, *, reply_mode: Literal["quote", "topic", "auto"] = "quote", **kwargs: Any
    ):
        super().__init__(**kwargs)
        self.reply_mode = reply_mode
        self.deliveries = NativeDeliveryStore(self.store)
        self.native_state = NativeCoworkerState(self.store)
        self._root_turns: dict[tuple[str, str], _RootTurn] = {}
        self._pending_collab: dict[str, set[str]] = {}
        self._held_completions: dict[str, tuple[str, str]] = {}
        self._session_locks: dict[int, RLock] = {}
        self._session_conditions: dict[int, Condition] = {}
        self.wait_for_queued_followups: Callable[[], bool] = lambda: True

    def _session_lock(self, session_id: int) -> RLock:
        with self._app_server_lock:
            return self._session_locks.setdefault(session_id, RLock())

    def _group_activation_id(self, chat_id: str) -> int | None:
        if chat_id != self.startup_chat_id:
            return None
        return super()._group_activation_id(chat_id)

    def _session_condition(self, session_id: int) -> Condition:
        with self._app_server_lock:
            condition = self._session_conditions.get(session_id)
            if condition is None:
                condition = Condition(self._session_lock(session_id))
                self._session_conditions[session_id] = condition
            return condition

    def _reserve_publication(self, session_id: int, event_id: int) -> None:
        condition = self._session_condition(session_id)
        deadline = time.monotonic() + 120 + 2 * self.config.lark_cli.timeout_seconds
        while True:
            can_wait = self.wait_for_queued_followups()
            if self.events.try_begin_native_publication(
                event_id, wait_for_queued=can_wait
            ):
                return
            remaining = deadline - time.monotonic()
            if not can_wait or remaining <= 0:
                raise ValueError(
                    "supplement outcome is uncertain; review before publishing"
                )
            condition.wait(timeout=min(1, remaining))

    def _turn_superseded(self, event_id: int) -> bool:
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT json_extract(raw_json, '$._runtime.superseded') FROM coworker_events
                   WHERE id = ? AND status = 'processing' AND json_valid(raw_json)""",
                (event_id,),
            ).fetchone()
        return row is not None and row[0] == 1

    def _classify_followup(
        self,
        policy: CoworkerConfig,
        prompt: str,
        cwd: str,
        *,
        fallback_prompt: Callable[[], str] | None = None,
    ) -> Literal["current", "unrelated", "uncertain"]:
        runtime = coworker_runtime_config(self.config, policy)
        classifier = CodexAppServer(
            config=runtime.agent_backend.codex,
            skill_names=[],
            explicit_context_paths=[],
            on_notification=lambda _event: None,
            request_timeout=10,
        )
        try:
            classifier.start()
            remaining = 30.0
            deadline = time.monotonic() + remaining
            while remaining > 0:
                result = classifier.run_turn(
                    prompt,
                    session_id=None,
                    cwd=cwd,
                    output_model=SteerDecision,
                    on_thread=lambda _thread: None,
                    private=True,
                    unload_delay_seconds=1,
                    dry_run_safety=True,
                    turn_timeout_seconds=remaining,
                    ephemeral=True,
                )
                if not result.ok:
                    break
                decision = SteerDecision.model_validate(result.json_data).decision
                if decision != "uncertain" or fallback_prompt is None:
                    return decision
                prompt = fallback_prompt()
                fallback_prompt = None
                remaining = deadline - time.monotonic()
        except (AppServerError, OSError, ValidationError):
            pass
        finally:
            classifier.close()
        return "uncertain"

    def on_app_server_notification(self, notification: dict[str, Any]) -> None:
        method = notification.get("method")
        params = _mapping(notification.get("params"))
        thread = params.get("threadId")
        if not isinstance(thread, str):
            return
        with self._app_server_lock:
            if method == "thread/closed":
                self._root_origins.pop(thread, None)
                unfinished: set[str] = set()
                for key in [key for key in self._root_turns if key[0] == thread]:
                    unfinished.update(self._root_turns.pop(key).pending_items)
                if unfinished:
                    for child_id, items in list(self._pending_collab.items()):
                        if items.isdisjoint(unfinished):
                            continue
                        items.difference_update(unfinished)
                        if not items:
                            self._pending_collab.pop(child_id)
                            self._held_completions.pop(child_id, None)
                            if self.events.mark_subagent_uncertain(child_id):
                                self.wake()
                return
            if method in {"turn/started", "turn/completed"}:
                turn = _mapping(params.get("turn"))
                turn_id = turn.get("id")
                if isinstance(turn_id, str) and thread in self._root_origins:
                    key = (thread, turn_id)
                    if method == "turn/started":
                        self._root_turns.setdefault(
                            key, _RootTurn(self._root_origins[thread])
                        )
                    elif (root := self._root_turns.get(key)) is not None:
                        root.finished = True
                        if not root.pending_items:
                            del self._root_turns[key]
                    return
                child = self.events.subagent_context(thread)
                if (
                    method == "turn/completed"
                    and isinstance(turn_id, str)
                    and child is not None
                    and child.get("child_turn_id") == turn_id
                    and self._pending_collab.get(thread)
                ):
                    self._held_completions[thread] = (
                        turn_id,
                        str(turn.get("status") or "unknown"),
                    )
                    return
                if (
                    isinstance(turn_id, str)
                    and child is not None
                    and child["status"] == "completed"
                ):
                    pending = self._child_turns.get(thread)
                    if method == "turn/started":
                        self._child_turns[thread] = (turn_id, None)
                    elif pending is not None and pending[0] == turn_id:
                        self._child_turns[thread] = (
                            turn_id,
                            str(turn.get("status") or "unknown"),
                        )
                    return
            item = _mapping(params.get("item"))
            if (
                method in {"item/started", "item/completed"}
                and item.get("type") == "collabAgentToolCall"
            ):
                self._bind_collab_item(thread, str(method), params, item)
                return
        super().on_app_server_notification(notification)

    def _bind_collab_item(
        self,
        parent_thread: str,
        method: str,
        params: dict[str, Any],
        item: dict[str, Any],
    ) -> None:
        if item.get("tool") not in {
            "spawnAgent",
            "sendInput",
            "followupTask",
            "resumeAgent",
        }:
            return
        if item.get("senderThreadId") != parent_thread:
            return
        item_id, turn_id = item.get("id"), params.get("turnId")
        if not isinstance(item_id, str) or not isinstance(turn_id, str):
            return
        key = (parent_thread, turn_id)
        root = self._root_turns.get(key)
        if root is None or item_id in root.completed_items:
            return
        origin = root.origin
        receivers = item.get("receiverThreadIds")
        receivers = cast(list[Any], receivers) if isinstance(receivers, list) else []
        if method == "item/started":
            root.pending_items.add(item_id)
            for child_id in receivers:
                if isinstance(child_id, str):
                    child = self.events.subagent_context(child_id)
                    if (
                        child is not None
                        and child["status"] == "running"
                        and child["origin_event_id"] != origin[1]
                    ):
                        self._pending_collab.setdefault(child_id, set()).add(item_id)
            return
        root.pending_items.discard(item_id)
        root.completed_items.add(item_id)
        if root.finished and not root.pending_items:
            del self._root_turns[key]
        for child_id in receivers:
            if not isinstance(child_id, str) or not child_id:
                continue
            child = self.events.subagent_context(child_id)
            successful = item.get("status") == "completed"
            if child is None and successful:
                self.events.register_subagent(
                    child_thread_id=child_id,
                    parent_thread_id=parent_thread,
                    agent_path=child_id,
                    session_id=origin[0],
                    origin_event_id=origin[1],
                    bot_open_id=origin[2],
                )
            elif child is not None and child["status"] == "completed" and successful:
                if not self.events.rebind_completed_subagent(
                    child_thread_id=child_id,
                    parent_thread_id=parent_thread,
                    session_id=origin[0],
                    origin_event_id=origin[1],
                    bot_open_id=origin[2],
                ):
                    raise RuntimeError("subagent could not be rebound to its parent")
            elif (
                child is not None
                and child["status"] == "running"
                and successful
                and not self.events.rebind_running_subagent(
                    child_thread_id=child_id,
                    parent_thread_id=parent_thread,
                    session_id=origin[0],
                    origin_event_id=origin[1],
                    bot_open_id=origin[2],
                )
            ):
                raise RuntimeError("running subagent belongs to another session")
            holds = self._pending_collab.get(child_id, set())
            holds.discard(item_id)
            if not holds:
                self._pending_collab.pop(child_id, None)
                held = self._held_completions.pop(child_id, None)
                if held is not None:
                    self.events.record_subagent_turn(child_id, held[0], outcome=held[1])
                    if self.events.complete_subagent(child_id):
                        self.wake()
            pending = self._child_turns.pop(child_id, None)
            if pending is not None and successful:
                self.events.record_subagent_turn(child_id, pending[0])
                if pending[1] is not None:
                    self.events.record_subagent_turn(
                        child_id, pending[0], outcome=pending[1]
                    )
                    if self.events.complete_subagent(child_id):
                        self.wake()

    def receive(self, raw: dict[str, Any], bot_open_id: str) -> None:
        policy = self.current_policy()
        event = _mapping(raw.get("event"))
        message = _mapping(event.get("message"))
        header = _mapping(raw.get("header"))
        if (
            policy is None
            or not self._matches_startup_policy(policy)
            or _sender_id(event) == bot_open_id
        ):
            return
        chat_type = message.get("chat_type")
        if chat_type == "group":
            if message.get("chat_id") != policy.chat_id:
                return
            activation = self._group_activation_id(str(policy.chat_id))
            if activation is None:
                return
        elif chat_type == "p2p":
            if (
                _sender_id(event) != self.config.owner.open_id
                or _mapping(event.get("sender")).get("sender_type") != "user"
            ):
                return
            activation = None
        else:
            return
        if header.get("app_id") not in (None, "", self.app_id):
            raise ValueError("message event belongs to a different app")
        safe_raw = dict(raw)
        safe_header = dict(header)
        safe_header.pop("token", None)
        safe_raw["header"] = safe_header
        if self.events.enqueue(
            app_id=self.app_id,
            bot_open_id=bot_open_id,
            raw=safe_raw,
            group_activation_id=activation,
            native=True,
        ):
            self.wake()

    def maintain_memory_if_due(self) -> bool:
        if self.execution_mode == "dry_run":
            return False
        return super().maintain_memory_if_due()

    def _reply_instructions(self) -> str:
        execution = (
            _DRY_RUN_GUIDANCE
            if self.execution_mode == "dry_run"
            else "按需使用原生工具完成请求。任务适合拆分时可使用子 Agent。"
            + _EVIDENCE_GUIDANCE
        )
        return (
            "你是飞书 AI 同事。"
            + _DELIVERY_GUIDANCE
            + execution
            + _REPLY_GUIDANCE
            + _CLARIFICATION_GUIDANCE
        )

    def _group_instructions(self, policy: CoworkerConfig) -> str:
        if self.execution_mode == "dry_run":
            return ""
        return (
            "可用时按需调用 read_feishu_topic 或 read_feishu_group_history 查看本群讨论；"
            "其他资料可按需使用已授权的工具查询。明确要求记住时，写入本群专用记忆 "
            f"{self.shared_memory(policy).path}。"
        )

    def run_memory_task(
        self, backend: CoworkerBackend, prompt: str, cwd: Path
    ) -> AgentRunResult:
        policy = self.current_policy()
        if policy is None:
            raise ValueError("coworker policy is unavailable")
        runtime = coworker_runtime_config(self.config, policy)
        server = CodexAppServer(
            config=runtime.agent_backend.codex,
            skill_names=[],
            explicit_context_paths=runtime.agent_backend.explicit_context.paths,
            on_notification=lambda _event: None,
            on_read_group=self.read_memory_history,
        )
        server.start()
        try:
            return server.run_turn(
                prompt,
                session_id=None,
                cwd=cwd,
                output_model=None,
                on_thread=lambda _thread: None,
                instructions=(
                    "只整理本群资料和本群记忆，不读取私聊资料或私聊笔记。"
                    "通过 read_feishu_group_history 按需读取本群历史。"
                ),
                private=True,
                unload_delay_seconds=1,
            )
        finally:
            server.close()

    def process_next(
        self, *, internal_only: bool = False, locating_only: bool = False
    ) -> bool:
        policy = self.current_policy()
        if (
            policy is None
            or not self._matches_startup_policy(policy)
            or not policy.chat_id
        ):
            return False
        if not internal_only and not locating_only:
            handoff = self.native_state.claim_handoff(
                app_id=self.app_id, chat_id=policy.chat_id
            )
            if handoff is not None:
                self._process_handoff(handoff)
                return True
            delivery = self.deliveries.claim(app_id=self.app_id, chat_id=policy.chat_id)
            if delivery is not None:
                self._deliver(delivery)
                return True
        event = self.events.claim_next(
            app_id=self.app_id,
            chat_id=policy.chat_id,
            internal_only=internal_only,
            native=True,
            locating_only=locating_only,
        )
        if event is None:
            return False
        try:
            if event.topic_key.startswith(f"{NATIVE_LOCATING_KEY}:"):
                envelope = _mapping(_mapping(event.raw.get("event")).get("event"))
                bot = str(event.raw.get("bot_open_id") or "")
                raw = event_message_from_raw(envelope, bot_id=bot)
                raw = self._enrich_message(raw, event.message_id, event.chat_id, bot)
                topic = self._topic_for_message(
                    raw, event.message_id, bot=bot, locating=True
                )
                if not self.events.route_native_event(
                    event, topic, queue_only=locating_only
                ):
                    return True
                event = replace(event, topic_key=topic)
            self._process_native(event)
        except _StartupPolicyChanged:
            self.events.defer_event(event.id)
            return False
        except _WatchDeferred:
            return True
        except _ReceiptPending as exc:
            self.events.defer_event_until(
                event.id,
                shift_instant(self.store.clock(), delta=timedelta(seconds=2)),
                private_receipt_topics=exc.private_topics,
            )
            return True
        except _SourceUnavailable as exc:
            self.events.retry_before_agent(
                event.id, error=f"{type(exc).__name__}: {exc}"
            )
        except Exception as exc:  # noqa: BLE001
            self.events.finish_event(
                event.id, error=f"{type(exc).__name__}: {exc}"[:500]
            )
            self.logger.error(
                "native_coworker_event_needs_review",
                data={"message_id": event.message_id, "error_type": type(exc).__name__},
            )
        else:
            self.events.finish_event(event.id)
        finally:
            self.deliveries.release_watch_batch(
                event.id, app_id=event.app_id, chat_id=event.chat_id
            )
        return True

    def _process_handoff(self, handoff: dict[str, Any]) -> None:
        session_id = int(handoff["session_id"])
        operation = str(handoff["operation"])
        try:
            if self.app_server is None:
                raise RuntimeError("Codex app-server is unavailable")
            thread_id = str(handoff["backend_session_id"])
            if operation == "releasing":
                self.app_server.release_thread(thread_id)
            else:
                self.app_server.reload_thread(
                    thread_id,
                    private=True,
                    cwd=str(handoff["agent_working_dir"]),
                    dry_run_safety=self.execution_mode == "dry_run",
                )
            self.native_state.finish_handoff(
                session_id, operation=operation, error=None
            )
        except Exception as exc:  # noqa: BLE001
            self.native_state.finish_handoff(
                session_id,
                operation=operation,
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            self.logger.error(
                "native_coworker_handoff_needs_review",
                data={
                    "session_id": session_id,
                    "error_type": type(exc).__name__,
                },
            )

    def process_next_steer(self) -> bool:
        policy, server = self.current_policy(), self.app_server
        if (
            policy is None
            or not policy.chat_id
            or server is None
            or not self._matches_startup_policy(policy)
        ):
            return False
        event = self.events.claim_next_steer(
            app_id=self.app_id, chat_id=policy.chat_id, native=True
        )
        if event is None:
            return False
        sending = False
        session_id: int | None = None
        origin_event_id: int | None = None
        source_revision: int | None = None
        steer_snapshot: dict[str, Any] | None = None
        session_lock: RLock | None = None
        turn_id: str | None = None
        try:
            with self.store.connect() as conn:
                current = conn.execute(
                    """SELECT e.id AS origin_event_id, e.message_id, e.raw_json,
                              s.id AS session_id, s.backend_session_id, s.agent_working_dir
                       FROM coworker_events e JOIN coworker_sessions s ON s.app_id = e.app_id
                         AND s.chat_id = e.chat_id AND s.thread_id = e.topic_key AND s.state = 'active'
                       WHERE e.app_id = ? AND e.chat_id = ? AND e.topic_key = ?
                         AND e.status = 'processing' AND e.runtime = 'native' LIMIT 1""",
                    (event.app_id, event.chat_id, event.topic_key),
                ).fetchone()
            if current is None or not current["backend_session_id"]:
                self.events.finish_steer(event.id, retry=True)
                return False
            envelope = _mapping(_mapping(event.raw.get("event")).get("event"))
            bot = str(event.raw.get("bot_open_id") or "")
            raw = event_message_from_raw(envelope, bot_id=bot)
            parent = raw.get("parent_id") or raw.get("reply_to_message_id")
            active_raw = _mapping(json.loads(str(current["raw_json"])))
            active_internal = _mapping(active_raw.get("_internal"))
            session_id = int(current["session_id"])
            origin_event_id = int(current["origin_event_id"])
            with self.store.connect() as conn:
                inherited = conn.execute(
                    """SELECT m.message_id, m.sender_id, m.text
                       FROM coworker_native_delivery_context c
                       JOIN messages m ON m.message_id = c.message_id
                       WHERE c.delivery_id = ? ORDER BY c.rowid""",
                    (active_internal.get("delivery_id"),),
                ).fetchall()
                accepted = conn.execute(
                    """SELECT m.message_id, m.sender_id, m.text FROM coworker_native_steers s
                       JOIN coworker_events e ON e.id = s.event_id
                       JOIN messages m ON m.message_id = e.message_id
                       WHERE s.origin_event_id = ? AND s.session_id = ? AND s.status = 'accepted'
                       ORDER BY e.received_at, e.id""",
                    (origin_event_id, session_id),
                ).fetchall()
            origins = {
                current["message_id"],
                active_internal.get("source_message_id"),
                active_internal.get("reply_target"),
            }
            origins.update(item["message_id"] for item in (*inherited, *accepted))
            if not isinstance(parent, str) or parent not in origins:
                self.events.skip_native_steer(event.id)
                return False
            self.events.associate_native_followup(event.id, origin_event_id, parent)
            thread_id = str(current["backend_session_id"])
            turn_id = server.active_turn_id(thread_id)
            session_lock = self._session_lock(session_id)
            session_lock.acquire()
            with self.store.connect() as conn:
                still_processing = conn.execute(
                    "SELECT 1 FROM coworker_events WHERE id = ? AND status = 'processing'",
                    (origin_event_id,),
                ).fetchone()
            if still_processing is None:
                self.events.finish_steer(event.id, retry=True)
                return False
            raw = self._enrich_message(raw, event.message_id, event.chat_id, bot)
            if (
                self._topic_for_message(raw, event.message_id, bot=bot)
                != event.topic_key
            ):
                raise ValueError("steer source changed location")
            private = event.topic_key.startswith(PRIVATE_TOPIC_PREFIX)
            message = self._message_normalizer(bot).normalize(
                raw, default_chat_type="p2p" if private else "group"
            )
            if message.at_all:
                self.events.skip_native_steer(event.id)
                return False
            if private and message.sender_id != self.config.owner.open_id:
                raise ValueError("private sender is not the owner")
            if private and ApprovalCommandService.is_approval_command(message.text):
                self.events.skip_native_steer(event.id)
                return False
            activation = None if private else self._group_activation_id(event.chat_id)
            if not private and (
                activation is None or activation != event.group_activation_id
            ):
                raise ValueError("group policy changed before steer")
            saved = self.store.upsert_message_with_revision(message)
            if saved.requires_confirmation or saved.is_deleted:
                raise ValueError("steer source revision cannot be verified")
            source_revision = saved.revision
            classified_snapshot = snapshot_evidence(message.raw)
            origin_message_id = (
                active_internal.get("source_message_id") or current["message_id"]
            )
            origin_message = self.store.get_message(str(origin_message_id))
            if origin_message is None:
                self.events.finish_steer(event.id, retry=True)
                return False
            current_request = {
                key: origin_message[key] for key in ("message_id", "sender_id", "text")
            }
            supplements = {
                item["message_id"]: dict(item)
                for item in (*inherited, *accepted)
                if item["message_id"] != current_request["message_id"]
            }
            recent_ids = {*list(supplements)[-_RECENT_SUPPLEMENTS:], parent}
            recent = [item for key, item in supplements.items() if key in recent_ids]
            new_message = _prompt_message(message) | {"reply_to_message_id": parent}

            def classification_prompt(*, use_full_context: bool) -> str:
                instructions = (
                    "判断新消息是否明确补充或更正当前任务。属于当前任务返回 current。"
                    "另一个任务返回 unrelated。证据不足时返回 uncertain。"
                )
                if not use_full_context:
                    instructions += "已接受的补充仅列出与直接引用有关的条目和近期条目。"
                return (
                    instructions
                    + "\n"
                    + _code_block(
                        json.dumps(
                            {
                                "current_request": current_request,
                                "accepted_supplements": list(supplements.values())
                                if use_full_context
                                else recent,
                                "new_message": new_message,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        "json",
                    )
                )

            has_full_context = len(recent) == len(supplements)
            classification = self._classify_followup(
                policy,
                classification_prompt(use_full_context=has_full_context),
                str(current["agent_working_dir"]),
                fallback_prompt=None
                if has_full_context
                else lambda: classification_prompt(use_full_context=True),
            )
            if classification != "current":
                self.events.skip_native_steer(event.id)
                return False
            if self.current_policy() != policy or (
                not private
                and self._group_activation_id(event.chat_id)
                != event.group_activation_id
            ):
                raise _StartupPolicyChanged
            raw = self._enrich_message(raw, event.message_id, event.chat_id, bot)
            message = self._message_normalizer(bot).normalize(
                raw, default_chat_type="p2p" if private else "group"
            )
            verified = self.store.upsert_message_with_revision(message)
            if (
                verified.requires_confirmation
                or verified.is_deleted
                or verified.revision != source_revision
                or snapshot_evidence(message.raw) != classified_snapshot
            ):
                self.events.skip_native_steer(event.id)
                return False
            if turn_id is None or server.active_turn_id(thread_id) != turn_id:
                self.events.defer_late_native_steer(event.id, origin_event_id)
                return False
            steer_snapshot = snapshot_evidence(message.raw)
            self.events.mark_steer_sending(event.id, native=True)
            sending = True
            prompt = "当前任务的补充信息：\n" + _code_block(
                json.dumps(_prompt_message(message), ensure_ascii=False), "json"
            )
            server.steer(thread_id, prompt, expected_turn_id=turn_id)
        except TurnNotActive:
            if session_lock is not None and origin_event_id is not None:
                self.events.defer_late_native_steer(event.id, origin_event_id)
            else:
                self.events.finish_steer(event.id, retry=True)
            return False
        except _SourceUnavailable as exc:
            self.events.retry_before_agent(
                event.id, error=f"{type(exc).__name__}: {exc}", steer=True
            )
        except Exception as exc:  # noqa: BLE001
            if (
                sending
                and session_id is not None
                and turn_id is not None
                and origin_event_id is not None
                and source_revision is not None
            ):
                self._record_steer(
                    event_id=event.id,
                    origin_event_id=origin_event_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    source_revision=source_revision,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                    snapshot=steer_snapshot,
                )
            else:
                self.events.skip_native_steer(event.id)
            return False
        else:
            self._record_steer(
                event_id=event.id,
                origin_event_id=origin_event_id,
                session_id=session_id,
                turn_id=turn_id,
                source_revision=source_revision,
                error=None,
                snapshot=steer_snapshot,
            )
            self.logger.info(
                "native_coworker_steer_accepted", data={"message_id": event.message_id}
            )
        finally:
            if session_lock is not None:
                if session_id is not None:
                    self._session_condition(session_id).notify_all()
                session_lock.release()
        return True

    def _record_steer(
        self,
        *,
        event_id: int,
        origin_event_id: int,
        session_id: int,
        turn_id: str,
        source_revision: int,
        error: str | None,
        snapshot: dict[str, Any] | None,
    ) -> None:
        for attempt in range(2):
            try:
                self.native_state.record_steer(
                    event_id=event_id,
                    origin_event_id=origin_event_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    source_revision=source_revision,
                    error=error,
                    snapshot=snapshot,
                )
                return
            except sqlite3.OperationalError:
                if attempt:
                    raise

    def _topic_for_message(
        self, raw: dict[str, Any], message_id: str, *, bot: str, locating: bool = False
    ) -> str:
        if raw.get("chat_type") == "p2p":
            return self._private_topic_for_message(raw, message_id, bot)
        thread = raw.get("thread_id")
        root = raw.get("root_id")
        aliases = [
            value for value in (thread, root) if isinstance(value, str) and value
        ]
        if self.chat_layout == "topic":
            if not aliases:
                aliases = [message_id]
        else:
            location = message_location(raw, chat_type="group")
            if location == "main":
                return GROUP_MAIN_KEY
            if location != "thread" or not isinstance(thread, str) or not thread:
                raise ValueError("message location cannot be verified")
        chat_id = raw.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("message chat is unavailable")
        if locating:
            self._hold_for_unconfirmed_opening(raw, chat_id, bot)
        return self.events.resolve_native_topic(
            app_id=self.app_id, chat_id=chat_id, aliases=aliases
        )

    def _private_topic_for_message(
        self, raw: dict[str, Any], message_id: str, bot: str
    ) -> str:
        independent = f"{PRIVATE_TOPIC_PREFIX}{message_id}"
        parent = raw.get("parent_id") or raw.get("reply_to_message_id")
        chat_id = raw.get("chat_id")
        if not isinstance(parent, str) or not parent or not isinstance(chat_id, str):
            return independent
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT e.topic_key FROM coworker_events e
                   WHERE e.app_id = ? AND e.chat_id = ? AND e.message_id = ?
                     AND e.runtime = 'native'
                   UNION ALL
                   SELECT e.topic_key FROM coworker_native_deliveries d
                   JOIN coworker_events e ON e.id = d.event_id
                   WHERE e.app_id = ? AND e.chat_id = ? AND d.sent_message_id = ?
                     AND d.status = 'sent'
                   UNION ALL
                   SELECT e.topic_key FROM dispatch_attempts attempt
                   JOIN actions a ON a.id = attempt.action_id
                   JOIN coworker_events e ON e.message_id = a.source_message_id
                   WHERE e.app_id = ? AND e.chat_id = ? AND e.runtime = 'native'
                     AND attempt.sent_message_id = ? AND attempt.status IN ('send_ok', 'readback_ok')
                     AND a.kind = 'send_reply' AND a.status = 'sent' AND a.execution_mode = 'production'
                     AND json_extract(a.payload_json, '$.identity') = 'bot'
                     AND json_extract(a.payload_json, '$.source') = 'coworker_approval'
                     AND json_extract(a.payload_json, '$.reply_destination.chat_id') = e.chat_id
                     AND json_extract(a.payload_json, '$.reply_destination.location') = 'main'
                   UNION ALL
                   SELECT NULL FROM dispatch_attempts attempt
                   JOIN actions a ON a.id = attempt.action_id
                   WHERE attempt.sent_message_id = ? AND attempt.status IN ('send_ok', 'readback_ok')
                     AND a.kind = 'owner_notification' AND a.execution_mode = 'production'
                   LIMIT 1""",
                (self.app_id, chat_id, parent) * 3 + (parent,),
            ).fetchone()
            if row is not None:
                if row["topic_key"] is None:
                    return independent  # an owner notification is a new message
                topic = str(row["topic_key"])
                if topic.startswith(f"{NATIVE_LOCATING_KEY}:"):
                    raise _ReceiptPending(
                        "quoted private request is still being located"
                    )
                return topic if topic.startswith(PRIVATE_TOPIC_PREFIX) else independent
            pending = conn.execute(
                """SELECT e.topic_key, d.sent_message_id, d.target_message_id
                   FROM coworker_native_deliveries d
                   JOIN coworker_events e ON e.id = d.event_id
                   WHERE e.app_id = ? AND e.chat_id = ?
                     AND d.status IN ('sending', 'needs_review')
                     AND (d.sent_message_id IS NULL OR d.sent_message_id = ?)""",
                (self.app_id, chat_id, parent),
            ).fetchall()
        topics = {str(item["topic_key"]) for item in pending if item["sent_message_id"]}
        unknown = [item for item in pending if not item["sent_message_id"]]
        if unknown and not topics:
            try:
                quoted = next(
                    iter(self._read_chat_messages(chat_id, [parent])), dict[str, Any]()
                )
            except _SourceUnavailable:
                quoted = {}
            sender = self._message_normalizer(bot).normalize(
                {**quoted, "message_id": parent}, default_chat_type="p2p"
            )
            target = (
                quoted.get("parent_id")
                or quoted.get("reply_to_message_id")
                or quoted.get("root_id")
            )
            if sender.sender_type != "user" and sender.sender_role != "owner_message":
                topics.update(
                    str(item["topic_key"])
                    for item in unknown
                    if not target or target == item["target_message_id"]
                )
        if topics:
            raise _ReceiptPending(
                "private reply receipt is not yet confirmed",
                private_topics=tuple(sorted(topics)),
            )
        return independent

    def _read_chat_messages(
        self, chat_id: str, message_ids: list[str]
    ) -> list[dict[str, Any]]:
        try:
            page = self.feishu.get_messages(as_identity="bot", message_ids=message_ids)
        except Exception as exc:
            raise _SourceUnavailable("referenced message unavailable") from exc
        return [
            item
            for item in page.items
            if item.get("message_id") in message_ids and item.get("chat_id") == chat_id
        ]

    def _hold_for_unconfirmed_opening(
        self, raw: dict[str, Any], chat_id: str, bot: str
    ) -> None:
        candidate_ids = list(
            dict.fromkeys(
                value
                for value in (
                    raw.get("parent_id") or raw.get("reply_to_message_id"),
                    raw.get("root_id"),
                )
                if isinstance(value, str) and value
            )
        )
        if not candidate_ids:
            return
        with self.store.connect() as conn:
            targets = {
                str(row[0])
                for row in conn.execute(
                    """SELECT d.target_message_id FROM coworker_native_deliveries d
                   JOIN coworker_events e ON e.id = d.event_id
                   WHERE e.app_id = ? AND e.chat_id = ?
                     AND d.purpose IN ('topic', 'transfer')
                     AND d.status = 'needs_review' AND d.sent_message_id IS NULL""",
                    (self.app_id, chat_id),
                )
            }
        if not targets or any(candidate in targets for candidate in candidate_ids):
            return  # the routing store handles known opening targets directly
        for item in self._read_chat_messages(chat_id, candidate_ids):
            if (
                item.get("thread_id") == raw.get("thread_id")
                and (
                    item.get("parent_id")
                    or item.get("reply_to_message_id")
                    or item.get("root_id")
                )
                in targets
            ):
                sender = self._message_normalizer(bot).normalize(
                    item, default_chat_type="group"
                )
                if (
                    sender.sender_id in (self.app_id, bot)
                    and sender.sender_role == "bot_message"
                ):
                    raise _ReceiptPending("topic opening receipt is not yet confirmed")

    def _is_own_reply(self, event: CoworkerEvent, parent_id: str | None) -> bool:
        if not parent_id:
            return False
        with self.store.connect() as conn:
            native = conn.execute(
                """SELECT 1 FROM coworker_native_deliveries d
                   JOIN coworker_events e ON e.id = d.event_id
                   WHERE e.app_id = ? AND e.chat_id = ? AND (e.topic_key = ? OR d.bound_topic_key = ?)
                     AND d.sent_message_id = ? AND d.status = 'sent'""",
                (
                    event.app_id,
                    event.chat_id,
                    event.topic_key,
                    event.topic_key,
                    parent_id,
                ),
            ).fetchone()
            legacy = conn.execute(
                """SELECT 1 FROM actions a
                   JOIN dispatch_attempts d ON d.action_id = a.id
                   JOIN coworker_events e ON e.message_id = a.source_message_id
                   WHERE e.app_id = ? AND e.chat_id = ?
                     AND a.kind = 'send_reply' AND a.status = 'sent' AND a.execution_mode = 'production'
                     AND json_extract(a.payload_json, '$.identity') = 'bot'
                     AND json_extract(a.payload_json, '$.business_mode') = 'coworker'
                     AND json_extract(a.payload_json, '$.reply_destination.chat_id') = ?
                     AND ((json_extract(a.payload_json, '$.reply_destination.location') = 'main' AND ? = ?)
                          OR (json_extract(a.payload_json, '$.reply_destination.location') = 'thread'
                              AND (json_extract(a.payload_json, '$.reply_destination.thread_id') = ?
                                   OR EXISTS (SELECT 1 FROM coworker_topic_aliases alias
                                      WHERE alias.app_id = e.app_id AND alias.chat_id = e.chat_id
                                        AND alias.alias = json_extract(a.payload_json, '$.reply_destination.thread_id')
                                        AND alias.topic_key = ?))))
                     AND d.status IN ('send_ok', 'readback_ok') AND d.sent_message_id = ?""",
                (
                    event.app_id,
                    event.chat_id,
                    event.chat_id,
                    event.topic_key,
                    GROUP_MAIN_KEY,
                    event.topic_key,
                    event.topic_key,
                    parent_id,
                ),
            ).fetchone()
        return native is not None or legacy is not None

    def _is_active_followup(self, event: CoworkerEvent, parent_id: str | None) -> bool:
        if not parent_id:
            return False
        with self.store.connect() as conn:
            return (
                conn.execute(
                    """SELECT 1 FROM coworker_events followup
                       JOIN coworker_events origin ON origin.id = json_extract(
                         followup.raw_json, '$._runtime.followup_origin')
                       WHERE followup.id = ? AND followup.status = 'processing'
                         AND json_extract(followup.raw_json, '$._runtime.followup_parent') = ?
                         AND origin.app_id = followup.app_id AND origin.chat_id = followup.chat_id
                         AND origin.topic_key = followup.topic_key AND origin.runtime = 'native'""",
                    (event.id, parent_id),
                ).fetchone()
                is not None
            )

    def _process_native(self, event: CoworkerEvent) -> None:
        if event.message_id.startswith(SUBAGENT_EVENT_PREFIX):
            self._process_native_completion(event)
            return
        if event.message_id.startswith("internal:transfer:"):
            self._process_native_transfer(event)
            return
        policy = self.current_policy()
        if policy is None or not self._matches_startup_policy(policy):
            raise _StartupPolicyChanged
        private = event.topic_key.startswith(PRIVATE_TOPIC_PREFIX)
        activation = None if private else self._group_activation_id(event.chat_id)
        if not private and (
            activation is None or activation != event.group_activation_id
        ):
            raise ValueError("coworker group policy changed after ingress")
        envelope = _mapping(_mapping(event.raw.get("event")).get("event"))
        bot = str(event.raw.get("bot_open_id") or "")
        raw = event_message_from_raw(envelope, bot_id=bot)
        raw = self._enrich_message(raw, event.message_id, event.chat_id, bot)
        if self._topic_for_message(raw, event.message_id, bot=bot) != event.topic_key:
            raise ValueError("message location changed after routing")
        message = self._message_normalizer(bot).normalize(
            raw, default_chat_type="p2p" if private else "group"
        )
        parent_id = raw.get("parent_id") or raw.get("reply_to_message_id")
        parent_id = parent_id if isinstance(parent_id, str) else None
        watch_trigger = False
        if private:
            if (
                message.chat_type != "p2p"
                or message.sender_id != self.config.owner.open_id
            ):
                raise ValueError("private sender is not the owner")
            if ApprovalCommandService.is_approval_command(message.text):
                return
        else:
            direct = (
                message.direct_mention
                or self._is_own_reply(event, parent_id)
                or self._is_active_followup(event, parent_id)
            )
            watched = (
                event.topic_key != GROUP_MAIN_KEY
                and self.native_state.is_watched(
                    app_id=event.app_id,
                    chat_id=event.chat_id,
                    topic_key=event.topic_key,
                    owner_open_id=self.config.owner.open_id,
                )
            )
            if message.at_all or not (direct or watched):
                return
            watch_trigger = watched and not direct
        batch: list[tuple[int, str, int]] = []
        batch_messages: list[dict[str, Any]] = []
        batch_snapshots: dict[str, dict[str, Any]] = {}
        if watch_trigger:
            batch, batch_messages, batch_snapshots = self._watch_batch(event, bot)
        upsert = self.store.upsert_message_with_revision(message)
        if upsert.requires_confirmation or upsert.is_deleted:
            raise ValueError("current message revision cannot be verified")
        working_dir = str(
            resolve_agent_working_dir(
                self.config.agent_backend.working_dir, self.base_dir
            )
        )
        session = self.events.get_or_create_session(
            app_id=event.app_id,
            chat_id=event.chat_id,
            topic_key=event.topic_key,
            provider="codex",
            working_dir=working_dir,
        )
        if self.execution_mode == "dry_run":
            self.native_state.require_dry_run_session(
                session.id, session.backend_session_id
            )
        server = self.app_server
        if server is None:
            raise RuntimeError("Codex app-server is unavailable")
        if private and session.backend_session_id:
            with self.store.connect() as conn:
                if (
                    conn.execute(
                        "SELECT 1 FROM coworker_sessions WHERE id != ? AND backend_session_id = ? LIMIT 1",
                        (session.id, session.backend_session_id),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        "private session is shared with another conversation"
                    )
        group_main = not private and event.topic_key == GROUP_MAIN_KEY
        can_watch = (
            not private
            and not group_main
            and message.sender_id == self.config.owner.open_id
        )
        output_model = (
            NativeMainOutput
            if group_main
            else NativeWatchOutput
            if can_watch
            else NativeReplyOutput
        )
        instructions = self._reply_instructions()
        if watch_trigger:
            instructions += "\n对关注产生的普通发言，只在相关时回复。"
        if can_watch:
            instructions += (
                "\nowner 明确要求关注或取消关注本话题时，返回 watch 或 unwatch，并填写确认正文。"
                "关注默认 24 小时，指定 watch_duration_seconds 时须为正数。"
            )
        if _mapping(event.raw.get("_runtime")).get("previous_unpublished") is True:
            instructions += "\n上一轮答复尚未发布，请结合补充重新完成并答复当前任务。"
        if group_main:
            choices = {
                "quote": "默认引用原消息回复；明确要求开话题时选择话题。",
                "topic": "主区新请求使用话题回复。",
                "auto": "根据任务选择引用回复或使用话题。",
            }
            instructions += (
                "\n"
                + choices[self.reply_mode]
                + "一次即可完成的结果可用 action=topic 在话题发布；需要在话题持续处理或明确要求开题时，"
                "先只返回 action=transfer 和简短接单内容，说明本任务的已知进展，话题建立后再执行任务。"
            )
        if private:
            instructions += "\n这是 owner 私聊。不要向转发来源发送消息。"
            if self.execution_mode != "dry_run":
                instructions += _PRIVATE_HISTORY_GUIDANCE
                notes = resolve_relative_path(policy.shared_memory_path, self.base_dir)
                notes = notes.with_name(notes.name + ".private")
                instructions += f"仅在明确要求记住时，写入私聊笔记 {notes}。"
        else:
            instructions += "\n" + self._group_instructions(policy)
        quote_source = None
        quoted_message = None
        if parent_id:
            quote = self._read_quote(
                parent_id,
                event.chat_id,
                bot,
                private=private,
                compact=not watch_trigger,
            )
            if quote is not None:
                quoted_message, quote_source = quote

        location = "private" if private else "main" if group_main else "thread"
        request_message = _prompt_message(message)
        details: dict[str, Any]
        if watch_trigger:
            details = {
                "owner_id": self.config.owner.open_id,
                "bot_id": bot,
                "chat_id": event.chat_id,
                "location": location,
                "message": request_message,
                "reply_to_message_id": parent_id,
            }
            if batch_messages:
                details["watched_updates"] = batch_messages
        else:
            request_message["sender_role"] = message.sender_role
            del request_message["sender_type"], request_message["is_deleted"]
            if message.message_type in (None, "text"):
                del request_message["message_type"]
            for key in ("sent_at", "mention_details", "resources"):
                if not request_message[key]:
                    del request_message[key]
            details = {"location": location, "message": request_message}
            if parent_id and quoted_message is None:
                details["reply_to_message_id"] = parent_id
            if message.direct_mention or (
                quoted_message is not None
                and (
                    quoted_message.get("sender_id") == bot
                    or bot in mention_identifiers(quoted_message.get("mentions"))
                    or bot in mention_identifiers(quoted_message.get("mention_details"))
                )
            ):
                details["bot_id"] = bot

        separators = None if watch_trigger else (",", ":")
        prompt = "## 当前请求\n\n" + _code_block(
            json.dumps(details, ensure_ascii=False, separators=separators), "json"
        )
        if quoted_message is not None:
            prompt += "\n引用内容：\n" + _code_block(
                json.dumps(quoted_message, ensure_ascii=False, separators=separators),
                "json",
            )
        self.events.mark_agent_started(event.id, quote=quote_source)
        result = server.run_turn(
            prompt,
            session_id=session.backend_session_id,
            cwd=session.agent_working_dir,
            output_model=output_model,
            on_thread=lambda thread_id: self._register_native_root(
                thread_id, session.id, event.id, bot
            ),
            instructions=instructions,
            private=True,
            unload_delay_seconds=1,
            dry_run_safety=self.execution_mode == "dry_run",
        )
        if not result.ok:
            raise RuntimeError(result.error or "Codex turn failed")
        if private:
            if self.current_policy() != policy:
                raise ValueError("coworker policy changed during the private turn")
        else:
            if activation is None:
                raise ValueError("group activation is unavailable")
            self._check_current_policy(policy, event.chat_id, activation)
        output = output_model.model_validate(result.json_data)
        with self._session_lock(session.id):
            self._reserve_publication(session.id, event.id)
            if self._turn_superseded(event.id):
                if batch:
                    self.deliveries.finish_silent_batch(
                        event.id, batch, batch_snapshots
                    )
                return
            self._require_certain_steers(event.id, session.id)
            if output.action == "silent":
                if batch:
                    self.deliveries.finish_silent_batch(
                        event.id, batch, batch_snapshots
                    )
                return
            if watch_trigger and not self.native_state.is_watched(
                app_id=event.app_id,
                chat_id=event.chat_id,
                topic_key=event.topic_key,
                owner_open_id=self.config.owner.open_id,
            ):
                if batch:
                    self.deliveries.finish_silent_batch(
                        event.id, batch, batch_snapshots
                    )
                return
            composed = SendComposer(owner_open_id=self.config.owner.open_id).compose(
                proposed_reply=output.text,
                reply_target=None,
                chat_type="p2p" if private else "group",
            )
            if composed.had_forbidden_mentions or not composed.text:
                raise ValueError("reply is empty or contains an unauthorized mention")
            purpose = (
                output.action if output.action in {"topic", "transfer"} else "reply"
            )
            if (
                purpose == "reply"
                and output.action == "reply"
                and group_main
                and self.reply_mode == "topic"
            ):
                purpose = "topic"
            if isinstance(output, NativeWatchOutput) and output.action in {
                "watch",
                "unwatch",
            }:
                if (
                    output.action == "watch"
                    and output.watch_duration_seconds is not None
                    and output.watch_duration_seconds <= 0
                ):
                    raise ValueError("watch duration must be positive")
                sources = [
                    (
                        message.message_id,
                        upsert.revision,
                        snapshot_evidence(message.raw),
                    ),
                    *self.deliveries.accepted_steer_sources(event.id, session.id),
                    *[
                        (message_id, rev, batch_snapshots[message_id])
                        for _, message_id, rev in batch
                    ],
                ]
                if quote_source:
                    sources.append(quote_source)
                try:
                    self._validate_source_snapshots(event.chat_id, sources)
                except _SourceUnavailable as exc:
                    raise ValueError("watch source cannot be verified") from exc
                self._check_current_policy(policy, event.chat_id, cast(int, activation))
                if output.action == "watch":
                    self.native_state.set_watch(
                        app_id=event.app_id,
                        chat_id=event.chat_id,
                        topic_key=event.topic_key,
                        owner_open_id=self.config.owner.open_id,
                        duration_seconds=24 * 60 * 60
                        if output.watch_duration_seconds is None
                        else output.watch_duration_seconds,
                    )
                else:
                    self.native_state.remove_watch(
                        app_id=event.app_id,
                        chat_id=event.chat_id,
                        topic_key=event.topic_key,
                    )
            disabled_reason = self._disabled_reply_reason(
                event.chat_id, private=private
            )
            if output.approval_required or disabled_reason:
                with self._session_lock(session.id):
                    self._queue_native_approval(
                        event=event,
                        message=message,
                        revision=upsert.revision,
                        working_dir=session.agent_working_dir,
                        session_id=session.id,
                        activation=activation,
                        target_message_id=message.message_id,
                        text=composed.text,
                        reason=output.approval_reason or disabled_reason,
                        quote_source=quote_source,
                        manual_only=private
                        or bool(batch)
                        or purpose in {"topic", "transfer"},
                    )
                    if batch:
                        self.deliveries.finish_silent_batch(
                            event.id, batch, batch_snapshots
                        )
                return
            self._queue_native_reply(
                event_id=event.id,
                session_id=session.id,
                source_revision=upsert.revision,
                target_message_id=event.message_id,
                text=composed.text,
                reply_in_thread=not private
                and (event.topic_key != GROUP_MAIN_KEY or purpose != "reply"),
                batch=batch,
                source_snapshot=snapshot_evidence(message.raw),
                context_snapshots=batch_snapshots,
                inherited_context=[quote_source] if quote_source else None,
                requires_watch=watch_trigger and output.action == "reply",
                purpose=purpose,
            )

    def _process_native_transfer(self, event: CoworkerEvent) -> None:
        policy = self.current_policy()
        if policy is None or not self._matches_startup_policy(policy):
            raise _StartupPolicyChanged
        activation = self._group_activation_id(event.chat_id)
        if activation is None or activation != event.group_activation_id:
            raise ValueError("group policy changed before topic transfer")
        internal = _mapping(event.raw.get("_internal"))
        origin_id, source_id = (
            internal.get("origin_event_id"),
            internal.get("source_message_id"),
        )
        target, bot = internal.get("reply_target"), event.raw.get("bot_open_id")
        if not isinstance(origin_id, int) or not all(
            isinstance(value, str) and value for value in (source_id, target, bot)
        ):
            raise ValueError("topic transfer context is incomplete")
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT m.raw_json, m.revision, m.is_deleted,
                          d.text AS acknowledgement, d.source_revision, d.source_snapshot_json
                   FROM coworker_native_transfers t
                   JOIN coworker_native_deliveries d ON d.id = t.delivery_id AND d.status = 'sent'
                   JOIN coworker_events source ON source.id = t.source_event_id
                   JOIN messages m ON m.message_id = source.message_id
                   WHERE t.event_id = ? AND t.source_event_id = ? AND t.reply_target = ?
                     AND source.app_id = ? AND source.chat_id = ? AND source.message_id = ?
                     AND source.topic_key = ?""",
                (
                    event.id,
                    origin_id,
                    target,
                    event.app_id,
                    event.chat_id,
                    source_id,
                    GROUP_MAIN_KEY,
                ),
            ).fetchone()
            context = conn.execute(
                """SELECT c.message_id, c.revision, c.snapshot_json, m.raw_json, m.revision AS current_revision,
                          m.is_deleted, m.chat_id
                   FROM coworker_native_transfers t
                   JOIN coworker_native_delivery_context c ON c.delivery_id = t.delivery_id
                   LEFT JOIN messages m ON m.message_id = c.message_id
                   WHERE t.event_id = ? ORDER BY c.rowid""",
                (event.id,),
            ).fetchall()
        if row is None:
            raise ValueError("topic transfer origin is unavailable")
        if row["revision"] != row["source_revision"] or row["is_deleted"]:
            raise ValueError("topic transfer source changed")
        source_raw = _mapping(json.loads(str(row["raw_json"])))
        source_snapshot = (
            _mapping(json.loads(str(row["source_snapshot_json"])))
            if row["source_snapshot_json"]
            else source_raw
        )
        source_raw = self._enrich_message(
            source_raw,
            str(source_id),
            event.chat_id,
            str(bot),
            expected_snapshot=source_snapshot,
        )
        message = self._message_normalizer(str(bot)).normalize(
            source_raw, default_chat_type="group"
        )
        upsert = self.store.upsert_message_with_revision(message)
        if (
            upsert.requires_confirmation
            or upsert.is_deleted
            or upsert.revision != row["source_revision"]
        ):
            raise ValueError("topic transfer source cannot be verified")
        supplements: list[dict[str, Any]] = []
        context_snapshots: dict[str, dict[str, Any]] = {}
        parent = source_raw.get("parent_id") or source_raw.get("reply_to_message_id")
        quote_source = quoted_message = None
        for item in context:
            if (
                item["current_revision"] != item["revision"]
                or item["is_deleted"]
                or item["chat_id"] != event.chat_id
            ):
                raise ValueError("topic transfer context changed")
            supplement_raw = _mapping(json.loads(str(item["raw_json"])))
            expected_snapshot = (
                _mapping(json.loads(str(item["snapshot_json"])))
                if item["snapshot_json"]
                else supplement_raw
            )
            supplement_raw = self._enrich_message(
                supplement_raw,
                str(item["message_id"]),
                event.chat_id,
                str(bot),
                expected_snapshot=expected_snapshot,
            )
            supplement = self._message_normalizer(str(bot)).normalize(
                supplement_raw, default_chat_type="group"
            )
            saved = self.store.upsert_message_with_revision(supplement)
            if (
                saved.requires_confirmation
                or saved.is_deleted
                or saved.revision != item["revision"]
            ):
                raise ValueError("topic transfer context changed")
            evidence = snapshot_evidence(supplement.raw)
            context_snapshots[supplement.message_id] = evidence
            if supplement.message_id == parent:
                quoted_message = _prompt_quote(supplement)
                quote_source = (supplement.message_id, saved.revision, evidence)
            else:
                supplements.append(_prompt_message(supplement))
        session = self.events.get_or_create_session(
            app_id=event.app_id,
            chat_id=event.chat_id,
            topic_key=event.topic_key,
            provider="codex",
            working_dir=str(
                resolve_agent_working_dir(
                    self.config.agent_backend.working_dir, self.base_dir
                )
            ),
        )
        if session.backend_session_id:
            raise ValueError("new topic already has a Codex session")
        if self.app_server is None:
            raise RuntimeError("Codex app-server is unavailable")
        instructions = (
            "飞书话题已经建立并确认接单。"
            + self._reply_instructions()
            + "\n"
            + self._group_instructions(policy)
        )
        prompt = "## 当前请求\n\n" + _code_block(
            json.dumps(_prompt_message(message), ensure_ascii=False), "json"
        )
        prompt += "\n已发送的接单内容：\n" + _code_block(str(row["acknowledgement"]))
        if supplements:
            prompt += "\n本任务已接收的补充：\n" + _code_block(
                json.dumps(supplements, ensure_ascii=False), "json"
            )
        if quote_source is None and isinstance(parent, str) and parent:
            quote = self._read_quote(parent, event.chat_id, str(bot), private=False)
            if quote is not None:
                quoted_message, quote_source = quote
        if quoted_message is not None:
            prompt += "\n直接引用：\n" + _code_block(
                json.dumps(quoted_message, ensure_ascii=False), "json"
            )
        self.events.mark_agent_started(event.id, quote=quote_source)
        result = self.app_server.run_turn(
            prompt,
            session_id=None,
            cwd=session.agent_working_dir,
            output_model=NativeReplyOutput,
            on_thread=lambda thread_id: self._register_native_root(
                thread_id, session.id, event.id, str(bot)
            ),
            instructions=instructions,
            private=True,
            unload_delay_seconds=1,
            dry_run_safety=self.execution_mode == "dry_run",
        )
        if not result.ok:
            raise RuntimeError(result.error or "topic transfer turn failed")
        self._check_current_policy(policy, event.chat_id, activation)
        output = NativeReplyOutput.model_validate(result.json_data)
        if output.action == "silent":
            return
        composed = SendComposer(owner_open_id=self.config.owner.open_id).compose(
            proposed_reply=output.text, reply_target=None, chat_type="group"
        )
        if composed.had_forbidden_mentions or not composed.text:
            raise ValueError("reply is empty or contains an unauthorized mention")
        disabled_reason = self._disabled_reply_reason(event.chat_id, private=False)
        if output.approval_required or disabled_reason:
            with self._session_lock(session.id):
                self._queue_native_approval(
                    event=event,
                    message=message,
                    revision=upsert.revision,
                    working_dir=session.agent_working_dir,
                    session_id=session.id,
                    activation=activation,
                    target_message_id=str(target),
                    text=composed.text,
                    reason=output.approval_reason or disabled_reason,
                    manual_only=True,
                    quote_source=quote_source,
                )
            return
        self._queue_native_reply(
            event_id=event.id,
            session_id=session.id,
            source_revision=upsert.revision,
            source_event_id=origin_id,
            source_message_id=str(source_id),
            source_snapshot=snapshot_evidence(message.raw),
            context_snapshots=context_snapshots,
            inherited_context=[quote_source]
            if quote_source and quote_source[0] not in context_snapshots
            else None,
            target_message_id=str(target),
            text=composed.text,
            reply_in_thread=True,
        )

    def _watch_batch(
        self, event: CoworkerEvent, bot: str
    ) -> tuple[
        list[tuple[int, str, int]], list[dict[str, Any]], dict[str, dict[str, Any]]
    ]:
        with self.store.connect() as conn:
            leader = conn.execute(
                "SELECT received_at FROM coworker_events WHERE id = ?", (event.id,)
            ).fetchone()
            rows = conn.execute(
                """SELECT id, message_id, raw_json, received_at, group_activation_id FROM coworker_events
                   WHERE app_id = ? AND chat_id = ? AND topic_key = ? AND status = 'queued'
                     AND runtime = 'native' ORDER BY received_at, id LIMIT 20""",
                (event.app_id, event.chat_id, event.topic_key),
            ).fetchall()
        if leader is None:
            raise ValueError("watch event disappeared")
        candidates: list[tuple[Any, dict[str, Any]]] = []
        last_received = str(leader["received_at"])
        for row in rows:
            try:
                payload = _mapping(json.loads(str(row["raw_json"])))
                envelope = _mapping(_mapping(payload.get("event")).get("event"))
                raw = event_message_from_raw(envelope, bot_id=bot)
                preliminary = self._message_normalizer(bot).normalize(
                    raw, default_chat_type="group"
                )
            except (TypeError, ValueError):
                break
            parent = raw.get("parent_id") or raw.get("reply_to_message_id")
            if (
                row["group_activation_id"] != event.group_activation_id
                or preliminary.direct_mention
                or preliminary.at_all
                or (isinstance(parent, str) and self._is_own_reply(event, parent))
                or (
                    parent == event.message_id
                    and (
                        _is_supplement(preliminary.text)
                        or _mapping(payload.get("_runtime")).get("followup_origin")
                        == event.id
                    )
                )
            ):
                break
            candidates.append((row, raw))
            last_received = str(row["received_at"])
        deadline = shift_instant(last_received, delta=timedelta(seconds=2))
        now, due = (
            parse_instant_or_none(self.store.clock()),
            parse_instant_or_none(deadline),
        )
        if now is None or due is None:
            raise ValueError("watch event timestamp is invalid")
        if now < due:
            self.events.defer_event_until(event.id, deadline)
            raise _WatchDeferred
        batch: list[tuple[int, str, int]] = []
        messages: list[dict[str, Any]] = []
        snapshots: dict[str, dict[str, Any]] = {}
        fresh = None
        if len(candidates) > 1:
            # Earlier candidates can truncate the batch before a failing read.
            with suppress(_SourceUnavailable):
                fresh = self._read_message_snapshots(
                    [str(row["message_id"]) for row, _raw in candidates]
                )
        for row, raw in candidates:
            message_id = str(row["message_id"])
            enriched = (
                self._enrich_message(raw, message_id, event.chat_id, bot)
                if fresh is None
                else self._enrich_message_from_snapshot(
                    raw, fresh.get(message_id), event.chat_id, bot
                )
            )
            if (
                self._topic_for_message(enriched, message_id, bot=bot)
                != event.topic_key
            ):
                break
            message = self._message_normalizer(bot).normalize(
                enriched, default_chat_type="group"
            )
            parent = enriched.get("parent_id") or enriched.get("reply_to_message_id")
            if (
                message.direct_mention
                or message.at_all
                or (parent == event.message_id and _is_supplement(message.text))
            ):
                break
            upsert = self.store.upsert_message_with_revision(message)
            if upsert.requires_confirmation or upsert.is_deleted:
                raise ValueError("watched message revision cannot be verified")
            batch.append((int(row["id"]), message.message_id, upsert.revision))
            messages.append(_prompt_message(message))
            snapshots[message.message_id] = snapshot_evidence(message.raw)
        reserved = self.deliveries.reserve_watch_batch(event.id, batch)
        return batch[:reserved], messages[:reserved], snapshots

    def _process_native_completion(self, event: CoworkerEvent) -> None:
        policy = self.current_policy()
        if policy is None or not self._matches_startup_policy(policy):
            raise _StartupPolicyChanged
        private = event.topic_key.startswith(PRIVATE_TOPIC_PREFIX)
        activation = None if private else self._group_activation_id(event.chat_id)
        if not private and (
            activation is None or activation != event.group_activation_id
        ):
            raise ValueError("group policy changed after the child started")
        internal = _mapping(event.raw.get("_internal"))
        origin_id, source_id, bot = (
            internal.get("origin_event_id"),
            internal.get("source_message_id"),
            event.raw.get("bot_open_id"),
        )
        source_event_id = internal.get("source_event_id", origin_id)
        reply_target = internal.get("reply_target") or source_id
        if (
            not isinstance(origin_id, int)
            or not isinstance(source_event_id, int)
            or not isinstance(source_id, str)
            or not isinstance(reply_target, str)
            or not isinstance(bot, str)
        ):
            raise ValueError("subagent completion context is incomplete")
        if not self.events.has_session(
            app_id=event.app_id,
            chat_id=event.chat_id,
            topic_key=event.topic_key,
            provider="codex",
        ):
            raise ValueError("subagent parent session is unavailable")
        session = self.events.get_or_create_session(
            app_id=event.app_id,
            chat_id=event.chat_id,
            topic_key=event.topic_key,
            provider="codex",
            working_dir=str(self.base_dir),
        )
        server = self.app_server
        if server is None or not session.backend_session_id:
            raise ValueError("subagent parent session is unavailable")
        if self.execution_mode == "dry_run":
            self.native_state.require_dry_run_session(
                session.id, session.backend_session_id
            )
        if private:
            with self.store.connect() as conn:
                if (
                    conn.execute(
                        "SELECT 1 FROM coworker_sessions WHERE id != ? AND backend_session_id = ? LIMIT 1",
                        (session.id, session.backend_session_id),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        "private session is shared with another conversation"
                    )
        with self.store.connect() as conn:
            source = conn.execute(
                """SELECT m.raw_json FROM coworker_events origin
                   JOIN coworker_events source ON source.id = ? AND source.app_id = origin.app_id
                     AND source.chat_id = origin.chat_id
                   JOIN messages m ON m.message_id = source.message_id
                   LEFT JOIN coworker_native_transfers t ON t.event_id = origin.id
                   WHERE origin.id = ? AND origin.app_id = ? AND origin.chat_id = ?
                     AND origin.topic_key = ? AND source.message_id = ?
                     AND (source.id = origin.id OR (t.source_event_id = source.id AND t.reply_target = ?))""",
                (
                    source_event_id,
                    origin_id,
                    event.app_id,
                    event.chat_id,
                    event.topic_key,
                    source_id,
                    reply_target,
                ),
            ).fetchone()
        if source is None:
            raise ValueError("subagent source is unavailable")
        raw = _mapping(json.loads(str(source["raw_json"])))
        raw = self._enrich_message(raw, source_id, event.chat_id, bot)
        message = self._message_normalizer(bot).normalize(
            raw, default_chat_type="p2p" if private else "group"
        )
        upsert = self.store.upsert_message_with_revision(message)
        if upsert.requires_confirmation or upsert.is_deleted:
            raise ValueError("subagent source revision cannot be verified")
        if private and (
            message.chat_type != "p2p" or message.sender_id != self.config.owner.open_id
        ):
            raise ValueError("private sender is not the owner")
        with self.store.connect() as conn:
            context = conn.execute(
                """SELECT c.message_id, c.revision, c.snapshot_json FROM coworker_native_delivery_context c
                   JOIN coworker_native_deliveries d ON d.id = c.delivery_id
                   WHERE (d.event_id = ? AND d.session_id = ?) OR d.id IN (
                     SELECT t.delivery_id FROM coworker_native_transfers t
                     WHERE t.event_id = ? AND t.delivery_id = ?)
                   UNION
                   SELECT e.message_id, s.source_revision, s.snapshot_json FROM coworker_native_steers s
                   JOIN coworker_events e ON e.id = s.event_id
                   WHERE s.origin_event_id = ? AND s.session_id = ? AND s.status = 'accepted'
                   UNION
                   SELECT json_extract(item.value, '$.message_id'), json_extract(item.value, '$.revision'),
                          json_extract(item.value, '$.snapshot')
                   FROM coworker_events origin,
                        json_each(CASE WHEN json_valid(origin.raw_json) THEN origin.raw_json ELSE '{}' END,
                                  '$._runtime.batch_context') item
                   WHERE origin.id = ?
                   UNION
                   SELECT json_extract(raw_json, '$._runtime.quote.message_id'),
                          json_extract(raw_json, '$._runtime.quote.revision'),
                          json_extract(raw_json, '$._runtime.quote.snapshot')
                   FROM coworker_events WHERE id = ? AND json_type(raw_json, '$._runtime.quote') = 'object'""",
                (
                    origin_id,
                    session.id,
                    origin_id,
                    internal.get("delivery_id"),
                    origin_id,
                    session.id,
                    origin_id,
                    origin_id,
                ),
            ).fetchall()
        inherited_context = [
            (str(item[0]), int(item[1]), json.loads(item[2]) if item[2] else None)
            for item in context
        ]
        self._validate_source_snapshots(event.chat_id, inherited_context)
        unique_context: dict[str, tuple[str, int, dict[str, Any] | None]] = {}
        for item in inherited_context:
            previous = unique_context.get(item[0])
            snapshot = item[2]
            if previous is None or (
                snapshot is not None
                and (
                    previous[2] is None
                    or (
                        str(snapshot.get("coworker_source_content_sha256")).startswith(
                            "v3:"
                        )
                        and not str(
                            previous[2].get("coworker_source_content_sha256")
                        ).startswith("v3:")
                    )
                )
            ):
                unique_context[item[0]] = item
        inherited_context = list(unique_context.values())
        notice = {
            "type": "internal_subagent_completion",
            "source_message_id": source_id,
            "current_source_message": _prompt_message(message),
            "agent_path": internal.get("agent_path"),
            "child_thread_id": internal.get("child_thread_id"),
            "child_turn_id": internal.get("child_turn_id"),
            "outcome": internal.get("outcome"),
        }
        prompt = (
            "子 Agent 已结束。查看结果并判断是否需要回复。"
            "根据当前原请求判断子 Agent 的结果是否仍然适用。"
            + _DELIVERY_GUIDANCE
            + (_DRY_RUN_GUIDANCE if self.execution_mode == "dry_run" else "")
            + _REPLY_GUIDANCE
            + "\n"
            + _code_block(json.dumps(notice, ensure_ascii=False), "json")
        )
        self.events.mark_agent_started(event.id)
        result = server.run_turn(
            prompt,
            session_id=session.backend_session_id,
            cwd=session.agent_working_dir,
            output_model=NativeReplyOutput,
            on_thread=lambda thread_id: self._register_native_root(
                thread_id, session.id, origin_id, bot
            ),
            private=True,
            unload_delay_seconds=1,
            dry_run_safety=self.execution_mode == "dry_run",
        )
        if not result.ok:
            raise RuntimeError(result.error or "subagent completion wake failed")
        if private:
            if self.current_policy() != policy:
                raise ValueError("coworker policy changed during the private turn")
        else:
            if activation is None:
                raise ValueError("group activation is unavailable")
            self._check_current_policy(policy, event.chat_id, activation)
        output = NativeReplyOutput.model_validate(result.json_data)
        if output.action == "silent":
            return
        composed = SendComposer(owner_open_id=self.config.owner.open_id).compose(
            proposed_reply=output.text,
            reply_target=None,
            chat_type="p2p" if private else "group",
        )
        if composed.had_forbidden_mentions or not composed.text:
            raise ValueError("reply is empty or contains an unauthorized mention")
        disabled_reason = self._disabled_reply_reason(event.chat_id, private=private)
        if output.approval_required or disabled_reason:
            parent = raw.get("parent_id") or raw.get("reply_to_message_id")
            quote_source = next(
                (
                    (message_id, revision, snapshot)
                    for message_id, revision, snapshot in inherited_context
                    if message_id == parent and snapshot is not None
                ),
                None,
            )
            with self._session_lock(session.id):
                self._queue_native_approval(
                    event=event,
                    message=message,
                    revision=upsert.revision,
                    working_dir=session.agent_working_dir,
                    session_id=session.id,
                    activation=activation,
                    target_message_id=reply_target,
                    text=composed.text,
                    reason=output.approval_reason or disabled_reason,
                    quote_source=quote_source,
                    manual_only=private
                    or reply_target != message.message_id
                    or bool(context),
                )
            return
        self._queue_native_reply(
            event_id=event.id,
            session_id=session.id,
            source_revision=upsert.revision,
            source_event_id=source_event_id,
            source_message_id=message.message_id,
            source_snapshot=snapshot_evidence(message.raw),
            inherited_context=inherited_context,
            target_message_id=reply_target,
            text=composed.text,
            reply_in_thread=not private and event.topic_key != GROUP_MAIN_KEY,
        )

    def _register_native_root(
        self, thread_id: str, session_id: int, origin_id: int, bot: str
    ) -> None:
        self._register_root(thread_id, session_id, origin_id, bot)
        if self.execution_mode == "dry_run":
            self.native_state.save_dry_run_session(session_id, thread_id)

    def _queue_native_reply(self, *, session_id: int, **kwargs: Any) -> None:
        with self._session_lock(session_id):
            self._reserve_publication(session_id, int(kwargs["event_id"]))
            if self._turn_superseded(int(kwargs["event_id"])):
                return
            self._require_certain_steers(int(kwargs["event_id"]), session_id)
            self.deliveries.queue_reply(session_id=session_id, **kwargs)

    def _require_certain_steers(self, event_id: int, session_id: int) -> None:
        with self.store.connect() as conn:
            uncertain = conn.execute(
                "SELECT 1 FROM coworker_native_steers WHERE origin_event_id = ? AND session_id = ? AND status = 'needs_review' LIMIT 1",
                (event_id, session_id),
            ).fetchone()
        if uncertain is not None:
            raise ValueError(
                "supplement outcome is uncertain; review before publishing"
            )

    def _read_quote(
        self,
        message_id: str,
        chat_id: str,
        bot: str,
        *,
        private: bool,
        compact: bool = False,
    ) -> tuple[dict[str, Any], tuple[str, int, dict[str, Any]]] | None:
        item = next(iter(self._read_chat_messages(chat_id, [message_id])), None)
        if item is None:
            return None
        message = self._message_normalizer(bot).normalize(
            item, default_chat_type="p2p" if private else "group"
        )
        fingerprint = message_content_fingerprint(item, include_mentions=True)
        if message.is_deleted or fingerprint is None:
            return None
        stored = self.store.get_message(message_id)
        if stored is None:
            saved = self.store.upsert_message_with_revision(message)
            if saved.is_deleted or saved.requires_confirmation:
                return None
            revision = saved.revision
        elif stored["chat_id"] == chat_id and not stored["is_deleted"]:
            revision = int(stored["revision"])
        else:
            return None
        quote = _prompt_quote(message)
        if compact:
            quote = {key: value for key, value in quote.items() if value is not None}
            quote["sender_role"] = message.sender_role
        return quote, (
            message_id,
            revision,
            {
                "coworker_source_content_sha256": fingerprint,
                "coworker_source_update_time": item.get("update_time"),
            },
        )

    def _authorized_group_reader(
        self, thread_id: str, turn_id: str, parent_active: bool
    ) -> tuple[CoworkerConfig, int]:
        policy = self.current_policy()
        if (
            policy is None
            or not self._matches_startup_policy(policy)
            or not policy.chat_id
        ):
            raise ValueError("group policy is unavailable")
        with self.store.connect() as conn:
            session = conn.execute(
                """SELECT s.chat_id, s.thread_id FROM coworker_sessions s
                   WHERE s.app_id = ? AND s.state = 'active'
                     AND ((? = 1 AND s.backend_session_id = ?)
                          OR (? = 0 AND EXISTS (SELECT 1 FROM coworker_subagents child
                              WHERE child.session_id = s.id AND child.child_thread_id = ?
                                AND child.child_turn_id = ? AND child.status = 'running')))""",
                (
                    self.app_id,
                    int(parent_active),
                    thread_id,
                    int(parent_active),
                    thread_id,
                    turn_id,
                ),
            ).fetchone()
        activation = self._group_activation_id(policy.chat_id)
        if (
            session is None
            or session["chat_id"] != policy.chat_id
            or str(session["thread_id"]).startswith(PRIVATE_TOPIC_PREFIX)
            or activation is None
        ):
            raise ValueError("topic read is outside the current group")
        return policy, activation

    def read_topic(
        self,
        thread_id: str,
        topic: str,
        page_token: str | None,
        turn_id: str = "",
        parent_active: bool = True,
    ) -> dict[str, Any]:
        policy, activation = self._authorized_group_reader(
            thread_id, turn_id, parent_active
        )
        if len(topic) > 1024 or (page_token is not None and len(page_token) > 1024):
            raise ValueError("topic read is outside the current group")
        page = self.feishu.list_thread_messages(
            thread_id=topic,
            page_token=page_token,
            page_size=50,
            as_identity="bot",
            order="asc",
        )
        return self._verified_group_page(policy, activation, page)

    def read_group_history(
        self, thread_id: str, page_token: str | None, turn_id: str, parent_active: bool
    ) -> dict[str, Any]:
        policy, activation = self._authorized_group_reader(
            thread_id, turn_id, parent_active
        )
        return self._read_group_history(policy, activation, page_token)

    def read_memory_history(
        self,
        _thread_id: str,
        page_token: str | None,
        _turn_id: str,
        parent_active: bool,
    ) -> dict[str, Any]:
        policy = self.current_policy()
        if (
            not parent_active
            or policy is None
            or not self._matches_startup_policy(policy)
            or not policy.chat_id
        ):
            raise ValueError("group policy is unavailable")
        activation = self._group_activation_id(policy.chat_id)
        if activation is None:
            raise ValueError("group policy is unavailable")
        return self._read_group_history(policy, activation, page_token)

    def _read_group_history(
        self, policy: CoworkerConfig, activation: int, page_token: str | None
    ) -> dict[str, Any]:
        if not policy.chat_id or (page_token is not None and len(page_token) > 1024):
            raise ValueError("invalid group history request")
        page = self.feishu.list_chat_messages(
            chat_id=policy.chat_id,
            start=None,
            end=None,
            page_token=page_token,
            page_size=50,
            as_identity="bot",
            order="desc",
        )
        return self._verified_group_page(policy, activation, page)

    def _verified_group_page(
        self, policy: CoworkerConfig, activation: int, page: MessagePage
    ) -> dict[str, Any]:
        if not policy.chat_id:
            raise ValueError("group policy is unavailable")
        missing = [
            item.get("message_id") for item in page.items if item.get("chat_id") is None
        ]
        if missing:
            if any(not isinstance(item, str) or not item for item in missing):
                raise ValueError("topic message identity is unavailable")
            snapshot = self.feishu.get_messages(
                as_identity="bot", message_ids=cast(list[str], missing)
            )
            chats = {
                item.get("message_id"): item.get("chat_id") for item in snapshot.items
            }
        else:
            chats = {}
        if any(
            (item.get("chat_id") or chats.get(item.get("message_id"))) != policy.chat_id
            for item in page.items
        ):
            raise ValueError("topic is outside the current group")
        self._check_current_policy(policy, policy.chat_id, activation)
        return {
            "messages": page.items,
            "next_page_token": page.next_page_token,
            "has_more": page.has_more,
        }

    def _disabled_reply_reason(self, chat_id: str, *, private: bool) -> str | None:
        if private:
            policy = self.store.get_product_policy() or {}
            return (
                None
                if _mapping(policy.get("reply_policy")).get("p2p_auto_reply") is True
                else "p2p_auto_reply_disabled"
            )
        return (
            None
            if PolicyResolver(self.store)
            .resolve_chat_policy(chat_id, "group")
            .auto_reply
            else "group_auto_reply_disabled"
        )

    def _queue_native_approval(
        self,
        *,
        event: CoworkerEvent,
        message: NormalizedMessage,
        revision: int,
        working_dir: str,
        session_id: int,
        activation: int | None,
        target_message_id: str,
        text: str,
        reason: str | None,
        manual_only: bool,
        quote_source: tuple[str, int, dict[str, Any]] | None = None,
    ) -> None:
        self._reserve_publication(session_id, event.id)
        if self._turn_superseded(event.id):
            return
        self._require_certain_steers(event.id, session_id)
        with self.store.connect() as conn:
            steered = conn.execute(
                "SELECT 1 FROM coworker_native_steers WHERE origin_event_id = ? AND session_id = ? AND status = 'accepted' LIMIT 1",
                (event.id, session_id),
            ).fetchone()
        manual_only = manual_only or steered is not None
        if target_message_id != message.message_id:
            internal = _mapping(event.raw.get("_internal"))
            with self.store.connect() as conn:
                anchor = conn.execute(
                    """SELECT d.thread_id FROM coworker_native_deliveries d
                       JOIN coworker_events origin ON origin.id = d.event_id
                       WHERE d.id = ? AND d.purpose = 'transfer' AND d.status = 'sent'
                         AND d.sent_message_id = ? AND d.bound_topic_key = ?
                         AND d.source_message_id = ? AND origin.chat_id = ?
                         AND origin.app_id = ?""",
                    (
                        internal.get("delivery_id"),
                        target_message_id,
                        event.topic_key,
                        message.message_id,
                        event.chat_id,
                        event.app_id,
                    ),
                ).fetchone()
            if anchor is None:
                raise ValueError(
                    "approval target is not bound to the transferred topic"
                )
            page = self.feishu.get_messages(
                as_identity="bot", message_ids=[target_message_id]
            )
            item = next(
                (
                    item
                    for item in page.items
                    if item.get("message_id") == target_message_id
                ),
                None,
            )
            if (
                item is None
                or item.get("chat_id") not in (None, event.chat_id)
                or item.get("thread_id") != anchor["thread_id"]
                or message_location(item, chat_type="group") != "thread"
            ):
                raise ValueError(
                    "approval target is unavailable in the transferred topic"
                )
            normalized = self._message_normalizer("").normalize(
                {**item, "chat_id": event.chat_id}, default_chat_type="group"
            )
            saved = self.store.upsert_message_with_revision(normalized)
            if saved.requires_confirmation or saved.is_deleted:
                raise ValueError("approval target is no longer available")
        target = self.store.get_message(target_message_id)
        candidate = (
            None
            if target is None
            else candidate_from_message(
                dict(target),
                expected_chat_id=event.chat_id,
                chat_type=message.chat_type,
            )
        )
        location = (
            "main"
            if message.chat_type == "p2p" or event.topic_key == GROUP_MAIN_KEY
            else "thread"
        )
        destination = None if candidate is None else candidate.destination(location)
        approvable = not manual_only and destination is not None
        task, _ = self.store.create_task_for_message_and_audit(
            message,
            watch_until=format_instant(utc_now()),
            agent_working_dir=working_dir,
            reason="coworker_turn",
            expected_group_activation_id=activation,
        )
        ApprovalService(
            store=self.store, config=self.config, execution_mode=self.execution_mode
        ).request_send_reply(
            task=task,
            reply_target_message_id=target_message_id,
            incoming_message_id=message.message_id,
            incoming_message_revision=revision,
            proposed_reply=text,
            final_reply=text,
            reason=reason or "needs_owner",
            reply_destination=destination if approvable else None,
            approvable=approvable,
            payload_extra={
                "identity": "bot",
                "business_mode": "coworker",
                "source": "coworker_approval",
                "coworker_snapshot_verified": message.raw.get(
                    "coworker_snapshot_verified"
                )
                is True,
                "coworker_source_update_time": message.raw.get(
                    "coworker_source_update_time"
                ),
                "coworker_source_content_sha256": message.raw.get(
                    "coworker_source_content_sha256"
                ),
                **(
                    {
                        "coworker_quote": {
                            "message_id": quote_source[0],
                            "revision": quote_source[1],
                            **quote_source[2],
                        }
                    }
                    if quote_source
                    else {}
                ),
            },
        )
        self.wake()

    def _deliver(self, delivery: NativeDelivery) -> None:
        sent_id: str | None = None
        topic_thread: str | None = None
        try:
            self._validate_delivery(delivery)
            sources = [
                (
                    delivery.source_message_id,
                    delivery.source_revision,
                    delivery.source_snapshot,
                ),
                *delivery.context_sources,
            ]
            client = cast(_ReplyClient, self.feishu)
            preview = client.reply_message(
                as_identity="bot",
                message_id=delivery.target_message_id,
                text=delivery.text,
                idempotency_key=delivery.idempotency_key,
                reply_in_thread=delivery.reply_in_thread,
                dry_run=True,
            )
            if not preview.ok:
                raise RuntimeError("reply dry-run validation failed")
            for attempt in range(3):
                try:
                    self._validate_source_snapshots(delivery.chat_id, sources)
                    break
                except _SourceUnavailable:
                    if attempt == 2:
                        raise
                    time.sleep(0.25 * (attempt + 1))
            self._validate_delivery(delivery)
            if self.execution_mode == "dry_run":
                self.deliveries.finish(delivery.id, status="previewed")
                return
            result = client.reply_message(
                as_identity="bot",
                message_id=delivery.target_message_id,
                text=delivery.text,
                idempotency_key=delivery.idempotency_key,
                reply_in_thread=delivery.reply_in_thread,
                dry_run=False,
            )
            if not result.ok:
                raise RuntimeError("reply outcome is uncertain")
            sent_id = _extract_message_id(
                result.json_data, exclude=delivery.target_message_id
            )
            if not sent_id:
                raise RuntimeError("reply receipt is missing")
            if delivery.purpose in {"topic", "transfer"}:
                page = self.feishu.get_messages(
                    as_identity="bot", message_ids=[sent_id]
                )
                receipt = next(
                    (item for item in page.items if item.get("message_id") == sent_id),
                    None,
                )
                if receipt is None or receipt.get("chat_id") not in (
                    None,
                    delivery.chat_id,
                ):
                    raise ValueError("topic receipt could not be read back")
                if message_location(receipt, chat_type="group") != "thread":
                    raise ValueError("topic receipt is not confirmed inside a thread")
                value = receipt.get("thread_id")
                if not isinstance(value, str) or not value:
                    raise ValueError("topic receipt has no thread ID")
                topic_thread = value
                self.deliveries.finish_topic(
                    delivery.id, sent_message_id=sent_id, thread_id=value
                )
            else:
                self.deliveries.finish(
                    delivery.id, status="sent", sent_message_id=sent_id
                )
        except _WatchExpired:
            self.deliveries.finish(
                delivery.id, status="discarded", error="watch_expired"
            )
        except Exception as exc:  # noqa: BLE001
            self.deliveries.finish(
                delivery.id,
                status="needs_review",
                sent_message_id=sent_id,
                thread_id=topic_thread,
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            self.logger.error(
                "native_coworker_delivery_needs_review",
                data={"delivery_id": delivery.id, "error_type": type(exc).__name__},
            )

    def _validate_source_snapshots(
        self,
        chat_id: str,
        sources: list[tuple[str, int, dict[str, Any] | None]],
    ) -> None:
        ids = list(dict.fromkeys(source_id for source_id, _, _ in sources))
        stored = {row["message_id"]: row for row in self.store.get_messages_by_ids(ids)}
        saved_snapshots: list[tuple[str, dict[str, Any]]] = []
        for source_id, revision, snapshot in sources:
            source = stored.get(source_id)
            if (
                source is None
                or source["chat_id"] != chat_id
                or source["revision"] != revision
                or source["is_deleted"]
            ):
                raise ValueError("reply source revision changed")
            saved = (
                snapshot
                if snapshot is not None
                else _mapping(json.loads(str(source["raw_json"])))
            )
            saved_snapshots.append((source_id, saved))
        fresh = self._read_message_snapshots(ids)
        for source_id, saved in saved_snapshots:
            current = fresh.get(source_id)
            if (
                current is None
                or current.get("chat_id") not in (None, chat_id)
                or current.get("is_deleted")
                or current.get("deleted")
                or not message_snapshot_matches(current, saved)
            ):
                raise ValueError("reply source cannot be verified")

    def _validate_delivery(self, delivery: NativeDelivery) -> None:
        policy = self.current_policy()
        if policy is None or not self._matches_startup_policy(policy):
            raise ValueError("coworker policy changed before delivery")
        private = delivery.topic_key.startswith(PRIVATE_TOPIC_PREFIX)
        if private:
            global_policy = self.store.get_product_policy() or {}
            if (
                _mapping(global_policy.get("reply_policy")).get("p2p_auto_reply")
                is not True
            ):
                raise ValueError("private auto reply is disabled")
        else:
            activation = self._group_activation_id(delivery.chat_id)
            if activation is None or activation != delivery.activation_id:
                raise ValueError("group policy was reactivated before delivery")
            chat = PolicyResolver(self.store).resolve_chat_policy(
                delivery.chat_id, "group"
            )
            if (
                not chat.auto_reply
                or not chat.bot_joined
                or chat.reply_identity not in {"bot", "bot_preferred"}
            ):
                raise ValueError("group policy does not allow a bot reply")
            if delivery.requires_watch and not self.native_state.is_watched(
                app_id=delivery.app_id,
                chat_id=delivery.chat_id,
                topic_key=delivery.topic_key,
                owner_open_id=self.config.owner.open_id,
            ):
                raise _WatchExpired("topic watch is no longer active")
        source_ids = [
            delivery.source_message_id,
            *(message_id for message_id, _, _ in delivery.context_sources),
        ]
        stored = {
            row["message_id"]: row for row in self.store.get_messages_by_ids(source_ids)
        }
        source = stored.get(delivery.source_message_id)
        if (
            source is None
            or source["revision"] != delivery.source_revision
            or source["is_deleted"]
            or source["chat_id"] != delivery.chat_id
            or (
                private
                and (
                    source["chat_type"] != "p2p"
                    or source["sender_id"] != self.config.owner.open_id
                )
            )
        ):
            raise ValueError("reply source revision changed")
        for message_id, revision, _snapshot in delivery.context_sources:
            context = stored.get(message_id)
            if (
                context is None
                or context["chat_id"] != delivery.chat_id
                or context["revision"] != revision
                or context["is_deleted"]
            ):
                raise ValueError("watch context revision changed")


class NativeCoworkerWorker(CoworkerWorker):
    service: NativeCoworkerService

    def _steer_worker_count(self, topic_workers: int) -> int:
        return topic_workers

    def _process_steering(self) -> bool:
        steered = self.service.process_next_steer()
        located = not self._stop.is_set() and self.service.process_next(
            locating_only=True
        )
        return steered or located

    def _can_wait_for_followups(self) -> bool:
        server = self.service.app_server
        return (
            not self._stop.is_set()
            and self.runtime_allows_processing()
            and (server is None or server.is_healthy())
        )

    def start(self) -> None:
        self.service.wait_for_queued_followups = self._can_wait_for_followups
        self.service.deliveries.recover_interrupted(app_id=self.service.app_id)
        self.service.native_state.recover_interrupted_handoffs(
            app_id=self.service.app_id
        )
        with self.service.store.connect() as conn:
            chats = conn.execute(
                "SELECT DISTINCT chat_id FROM coworker_events WHERE app_id = ? AND runtime = 'native'",
                (self.service.app_id,),
            ).fetchall()
        for row in chats:
            self.service.events.recover_interrupted(
                app_id=self.service.app_id, chat_id=str(row[0])
            )
        super().start()

    def _running_subagents(self, chat_id: str) -> list[dict[str, Any]]:
        return self.service.events.running_subagents(
            app_id=self.service.app_id, chat_id=None
        )
