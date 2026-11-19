from __future__ import annotations

import json
import shutil
import sys
from _thread import RLock
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict

from .agent_backend import AgentRunResult
from .agent_backend_factory import coworker_runtime_config
from .codex_app_server import CodexAppServer, TurnNotActive
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
from .coworker_native_store import NativeDelivery, NativeDeliveryStore
from .coworker_store import (
    GROUP_MAIN_KEY,
    NATIVE_LOCATING_KEY,
    SUBAGENT_EVENT_PREFIX,
    CoworkerEvent,
)
from .dispatcher import _extract_message_id  # pyright: ignore[reportPrivateUsage]
from .message_normalization import MessageNormalizer, message_content_fingerprint
from .paths import resolve_agent_working_dir, resolve_relative_path
from .policy import PolicyResolver
from .processing import ApprovalService, SendComposer
from .reply_destination import candidate_from_message, message_location
from .time_utils import format_instant, parse_instant_or_none, shift_instant, utc_now
from .types import LarkCliResult, MessagePage, NormalizedMessage

PRIVATE_TOPIC_PREFIX = "__private__:"


def _is_supplement(text: str) -> bool:
    return (
        text.lstrip()
        .lower()
        .startswith(("补充", "更正", "追加", "supplement:", "correction:"))
    )


class _WatchDeferred(Exception):
    pass


class _ReceiptPending(Exception):
    pass


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


class NativeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["reply", "silent", "watch", "unwatch", "topic", "transfer"]
    text: str
    watch_duration_seconds: int | None = None
    approval_required: bool = False
    approval_reason: str | None = None


class NativeCoworkerService(CoworkerService):
    """Codex execution with project-owned, receipt-backed replies."""

    def __init__(
        self, *, reply_mode: Literal["quote", "topic", "auto"] = "quote", **kwargs: Any
    ):
        super().__init__(**kwargs)
        self.reply_mode = reply_mode
        self.deliveries = NativeDeliveryStore(self.store)
        self.native_state = NativeCoworkerState(self.store)
        self._root_turn_origins: dict[tuple[str, str], tuple[int, int, str]] = {}
        self._collab_items: set[str] = set()
        self._pending_collab: dict[str, set[str]] = {}
        self._held_completions: dict[str, tuple[str, str]] = {}
        self._session_locks: dict[int, RLock] = {}
        policy = self.current_policy()
        self.memory_root = (
            None
            if policy is None
            else resolve_relative_path(
                policy.shared_memory_path, self.base_dir
            ).parent.resolve()
        )

    def _session_lock(self, session_id: int) -> RLock:
        with self._app_server_lock:
            return self._session_locks.setdefault(session_id, RLock())

    def validate_workspace(self, cwd: str | Path) -> None:
        if self.execution_mode == "dry_run":
            return
        workspace = Path(cwd).expanduser().resolve()
        policy = self.current_policy()
        memory_root = (
            None
            if policy is None
            else resolve_relative_path(
                policy.shared_memory_path, self.base_dir
            ).parent.resolve()
        )
        if memory_root is None or memory_root != self.memory_root:
            raise ValueError("coworker memory path changed; restart the native runtime")
        protected = [
            self.base_dir,
            self.store.path,
            Path(__file__),
            Path(sys.executable),
        ]
        for command, command_base in (
            (self.config.lark_cli.path or "lark-cli", self.base_dir),
            (self.config.agent_backend.codex.path or "codex", Path.cwd()),
            ("node", Path.cwd()),
        ):
            if "/" in command:
                executable = Path(command).expanduser()
                protected.append(
                    executable
                    if executable.is_absolute()
                    else command_base / executable
                )
            elif found := shutil.which(command):
                protected.append(Path(found))
        if any(
            path.absolute().is_relative_to(root) or path.resolve().is_relative_to(root)
            for path in protected
            for root in (workspace, memory_root)
        ):
            raise ValueError(
                "Codex working directory and memory directory must not contain the project configuration, database, runtime code, or executables"
            )
        memory_root.mkdir(parents=True, exist_ok=True)

    def on_app_server_notification(self, notification: dict[str, Any]) -> None:
        method = notification.get("method")
        params = _mapping(notification.get("params"))
        thread = params.get("threadId")
        if not isinstance(thread, str):
            return
        with self._app_server_lock:
            if method in {"turn/started", "turn/completed"}:
                turn = _mapping(params.get("turn"))
                turn_id = turn.get("id")
                if isinstance(turn_id, str) and thread in self._root_origins:
                    if method == "turn/started":
                        self._root_turn_origins[(thread, turn_id)] = self._root_origins[
                            thread
                        ]
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
        if (
            not isinstance(item_id, str)
            or item_id in self._collab_items
            or not isinstance(turn_id, str)
        ):
            return
        origin = self._root_turn_origins.get((parent_thread, turn_id))
        receivers = item.get("receiverThreadIds")
        if origin is None or not isinstance(receivers, list) or not receivers:
            return
        receivers = cast(list[Any], receivers)
        if method == "item/started":
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
        self._collab_items.add(item_id)
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

    def run_memory_task(
        self, backend: CoworkerBackend, prompt: str, cwd: Path
    ) -> AgentRunResult:
        policy = self.current_policy()
        if policy is None:
            raise ValueError("coworker policy is unavailable")
        runtime = coworker_runtime_config(self.config, policy)
        server = CodexAppServer(
            config=runtime.agent_backend.codex,
            skill_names=runtime.agent_backend.codex.skills,
            explicit_context_paths=runtime.agent_backend.explicit_context.paths,
            on_notification=lambda _event: None,
            confine_production=True,
            writable_roots=[] if self.memory_root is None else [self.memory_root],
            on_read_group=self.read_memory_history,
        )
        self.validate_workspace(cwd)
        server.start()
        try:
            return server.run_turn(
                prompt + "\n只整理本群资料和本群记忆，不读取私聊资料或私聊笔记。"
                "通过 read_feishu_group_history 按需读取本群历史。",
                session_id=None,
                cwd=cwd,
                output_model=None,
                on_thread=lambda _thread: None,
                private=True,
                unload_delay_seconds=1,
            )
        finally:
            server.close()

    def process_next(self, *, internal_only: bool = False) -> bool:
        policy = self.current_policy()
        if (
            policy is None
            or not self._matches_startup_policy(policy)
            or not policy.chat_id
        ):
            return False
        if not internal_only:
            handoff = self.native_state.claim_handoff(app_id=self.app_id)
            if handoff is not None:
                self._process_handoff(handoff)
                return True
            delivery = self.deliveries.claim(app_id=self.app_id)
            if delivery is not None:
                self._deliver(delivery)
                return True
        event = self.events.claim_next(
            app_id=self.app_id,
            internal_only=internal_only,
            native=True,
        )
        if event is None:
            return False
        try:
            if event.topic_key.startswith(f"{NATIVE_LOCATING_KEY}:"):
                envelope = _mapping(_mapping(event.raw.get("event")).get("event"))
                bot = str(event.raw.get("bot_open_id") or "")
                raw = event_message_from_raw(envelope, bot_id=bot)
                raw = self._enrich_message(raw, event.message_id, event.chat_id, bot)
                topic = self._topic_for_message(raw, event.message_id)
                if not self.events.route_native_event(event, topic):
                    return True
                event = replace(event, topic_key=topic)
            self._process_native(event)
        except _StartupPolicyChanged:
            self.events.defer_event(event.id)
            return False
        except _WatchDeferred:
            return True
        except _ReceiptPending:
            self.events.defer_event_until(
                event.id, shift_instant(self.store.clock(), delta=timedelta(seconds=2))
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
                self.validate_workspace(str(handoff["agent_working_dir"]))
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
        if policy is None or server is None or not self._matches_startup_policy(policy):
            return False
        event = self.events.claim_next_steer(
            app_id=self.app_id, chat_id=None, native=True
        )
        if event is None:
            return False
        sending = False
        session_id: int | None = None
        origin_event_id: int | None = None
        source_revision: int | None = None
        session_lock: RLock | None = None
        turn_id: str | None = None
        try:
            with self.store.connect() as conn:
                current = conn.execute(
                    """SELECT e.id AS origin_event_id, e.message_id, e.raw_json,
                              s.id AS session_id, s.backend_session_id
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
            raw = self._enrich_message(raw, event.message_id, event.chat_id, bot)
            if self._topic_for_message(raw, event.message_id) != event.topic_key:
                raise ValueError("steer source changed location")
            private = event.topic_key.startswith(PRIVATE_TOPIC_PREFIX)
            message = MessageNormalizer(owner_open_id=bot).normalize(
                raw, default_chat_type="p2p" if private else "group"
            )
            parent = raw.get("parent_id") or raw.get("reply_to_message_id")
            active_raw = _mapping(json.loads(str(current["raw_json"])))
            active_internal = _mapping(active_raw.get("_internal"))
            origins = {
                current["message_id"],
                active_internal.get("source_message_id"),
                active_internal.get("reply_target"),
            }
            if (
                not isinstance(parent, str)
                or parent not in origins
                or message.at_all
                or not _is_supplement(message.text)
            ):
                self.events.skip_native_steer(event.id)
                return False
            if private and message.sender_id != self.config.owner.open_id:
                raise ValueError("private sender is not the owner")
            if (
                not private
                and self._group_activation_id(event.chat_id)
                != event.group_activation_id
            ):
                raise ValueError("group policy changed before steer")
            saved = self.store.upsert_message_with_revision(message)
            if saved.requires_confirmation or saved.is_deleted:
                raise ValueError("steer source revision cannot be verified")
            thread_id = str(current["backend_session_id"])
            session_id = int(current["session_id"])
            origin_event_id = int(current["origin_event_id"])
            source_revision = saved.revision
            turn_id = server.active_turn_id(thread_id)
            if not turn_id:
                self.events.finish_steer(event.id, retry=True)
                return False
            session_lock = self._session_lock(session_id)
            session_lock.acquire()
            self.events.mark_steer_sending(event.id, native=True)
            sending = True
            prompt = "当前任务的补充信息：\n" + _code_block(
                json.dumps(_prompt_message(message), ensure_ascii=False), "json"
            )
            server.steer(thread_id, prompt, expected_turn_id=turn_id)
        except TurnNotActive:
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
                self.native_state.record_steer(
                    event_id=event.id,
                    origin_event_id=origin_event_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    source_revision=source_revision,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
            else:
                self.events.finish_steer(event.id, retry=True)
            return False
        else:
            self.native_state.record_steer(
                event_id=event.id,
                origin_event_id=origin_event_id,
                session_id=session_id,
                turn_id=turn_id,
                source_revision=source_revision,
                error=None,
            )
            self.logger.info(
                "native_coworker_steer_accepted", data={"message_id": event.message_id}
            )
        finally:
            if session_lock is not None:
                session_lock.release()
        return True

    def _topic_for_message(self, raw: dict[str, Any], message_id: str) -> str:
        if raw.get("chat_type") == "p2p":
            parent = raw.get("parent_id") or raw.get("reply_to_message_id")
            if isinstance(parent, str) and parent:
                with self.store.connect() as conn:
                    row = conn.execute(
                        """SELECT e.topic_key FROM coworker_events e
                           WHERE e.app_id = ? AND e.chat_id = ? AND e.message_id = ?
                             AND e.runtime = 'native'
                           UNION ALL
                           SELECT e.topic_key FROM coworker_native_deliveries d
                           JOIN coworker_events e ON e.id = d.event_id
                           WHERE e.app_id = ? AND e.chat_id = ? AND d.sent_message_id = ?
                             AND d.status = 'sent' LIMIT 1""",
                        (
                            self.app_id,
                            raw.get("chat_id"),
                            parent,
                            self.app_id,
                            raw.get("chat_id"),
                            parent,
                        ),
                    ).fetchone()
                if row is not None:
                    topic = str(row["topic_key"])
                    if topic.startswith(f"{NATIVE_LOCATING_KEY}:"):
                        raise _ReceiptPending(
                            "quoted private request is still being located"
                        )
                    if topic.startswith(PRIVATE_TOPIC_PREFIX):
                        return topic
                else:
                    with self.store.connect() as conn:
                        pending = conn.execute(
                            """SELECT 1 FROM coworker_native_deliveries d
                               JOIN coworker_events e ON e.id = d.event_id
                               WHERE e.app_id = ? AND e.chat_id = ?
                                 AND d.status IN ('sending', 'needs_review') LIMIT 1""",
                            (self.app_id, raw.get("chat_id")),
                        ).fetchone()
                    if pending is not None:
                        raise _ReceiptPending(
                            "private reply receipt is not yet confirmed"
                        )
            return f"{PRIVATE_TOPIC_PREFIX}{message_id}"
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
        return self.events.resolve_native_topic(
            app_id=self.app_id, chat_id=chat_id, aliases=aliases
        )

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
        if self._topic_for_message(raw, event.message_id) != event.topic_key:
            raise ValueError("message location changed after routing")
        message = MessageNormalizer(owner_open_id=bot).normalize(
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
        else:
            direct = message.direct_mention or self._is_own_reply(event, parent_id)
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
        if watch_trigger:
            batch, batch_messages = self._watch_batch(event, bot)
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
        self.validate_workspace(session.agent_working_dir)
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
        details: dict[str, Any] = {
            "owner_id": self.config.owner.open_id,
            "bot_id": bot,
            "chat_id": event.chat_id,
            "location": "private"
            if private
            else "main"
            if event.topic_key == GROUP_MAIN_KEY
            else "thread",
            "message": _prompt_message(message),
            "reply_to_message_id": parent_id,
        }
        if batch_messages:
            details["watched_updates"] = batch_messages
        prompt = (
            "你是飞书 AI 同事。使用原生工具和子 Agent 完成请求；项目负责发布最终答复。"
            "飞书消息只能由项目发布，不要使用工具自行发送或回复。"
            "返回 action=reply 和自然文本，或 action=silent。"
            "owner 明确要求关注或取消关注本话题时，返回 watch 或 unwatch；"
            "watch_duration_seconds 可指定秒数，默认 24 小时。"
            "需要 owner 判断、授权或替其承诺时，设置 approval_required=true 并写明 approval_reason。"
            "对关注产生的普通发言，只在相关时回复。\n"
            + _code_block(json.dumps(details, ensure_ascii=False), "json")
        )
        if not private and event.topic_key == GROUP_MAIN_KEY:
            choices = {
                "quote": "默认引用原消息回复；明确要求开话题时选择话题。",
                "topic": "主区新请求使用话题回复。",
                "auto": "根据任务选择引用回复或使用话题。",
            }
            prompt += (
                "\n"
                + choices[self.reply_mode]
                + "一次即可完成的结果可用 action=topic 在话题发布；需要在话题持续处理或明确要求开题时，"
                "先只返回 action=transfer 和简短接单内容，话题建立后再执行任务。"
            )
        if private:
            notes = resolve_relative_path(policy.shared_memory_path, self.base_dir)
            suffix = (
                ".private.dry-run" if self.execution_mode == "dry_run" else ".private"
            )
            notes = notes.with_name(notes.name + suffix)
            prompt += (
                "\n这是 owner 私聊；仅在明确要求记住时写入私聊笔记 "
                + str(notes)
                + "。不要向转发来源发送消息；目标不清楚时询问。"
            )
        else:
            prompt += (
                "\n群内答复和整理使用本群资料；明确要求记住时使用本群专用记忆 "
                + str(self.shared_memory(policy).path)
                + "，不要读取私聊笔记。"
            )
            if self.execution_mode != "dry_run":
                prompt += "按需使用 read_feishu_topic 读取本群话题，或使用 read_feishu_group_history 读取本群历史。"
        if parent_id:
            quote = self._quoted_message(parent_id)
            if quote is not None:
                prompt += "\n引用内容：\n" + _code_block(
                    json.dumps(quote, ensure_ascii=False), "json"
                )
        self.events.mark_agent_started(event.id)
        result = server.run_turn(
            prompt,
            session_id=session.backend_session_id,
            cwd=session.agent_working_dir,
            output_model=NativeOutput,
            on_thread=lambda thread_id: self._register_native_root(
                thread_id, session.id, event.id, bot
            ),
            private=True,
            unload_delay_seconds=1,
            dry_run_safety=self.execution_mode == "dry_run",
        )
        if not result.ok:
            raise RuntimeError(result.error or "Codex turn failed")
        if result.session_id:
            self.events.save_session_id(session.id, result.session_id)
        if private:
            if self.current_policy() != policy:
                raise ValueError("coworker policy changed during the private turn")
        else:
            if activation is None:
                raise ValueError("group activation is unavailable")
            self._check_current_policy(policy, event.chat_id, activation)
        output = NativeOutput.model_validate(result.json_data)
        with self._session_lock(session.id):
            self._require_certain_steers(event.id, session.id)
            if output.action == "silent":
                if batch:
                    self.deliveries.finish_silent_batch(event.id, batch)
                return
            if watch_trigger and not self.native_state.is_watched(
                app_id=event.app_id,
                chat_id=event.chat_id,
                topic_key=event.topic_key,
                owner_open_id=self.config.owner.open_id,
            ):
                if batch:
                    self.deliveries.finish_silent_batch(event.id, batch)
                return
            if output.action in {"watch", "unwatch"}:
                if (
                    private
                    or event.topic_key == GROUP_MAIN_KEY
                    or message.sender_id != self.config.owner.open_id
                ):
                    raise ValueError("only the owner can change a real topic watch")
                if (
                    output.action == "watch"
                    and output.watch_duration_seconds is not None
                    and output.watch_duration_seconds <= 0
                ):
                    raise ValueError("watch duration must be positive")
            composed = SendComposer(owner_open_id=self.config.owner.open_id).compose(
                proposed_reply=output.text,
                reply_target=None,
                chat_type="p2p" if private else "group",
            )
            if composed.had_forbidden_mentions or not composed.text:
                raise ValueError("reply is empty or contains an unauthorized mention")
            if output.action in {"topic", "transfer"} and (
                private or event.topic_key != GROUP_MAIN_KEY
            ):
                raise ValueError("only group main messages can open a new topic")
            purpose = (
                output.action if output.action in {"topic", "transfer"} else "reply"
            )
            if (
                purpose == "reply"
                and output.action == "reply"
                and not private
                and event.topic_key == GROUP_MAIN_KEY
                and self.reply_mode == "topic"
            ):
                purpose = "topic"
            if output.action in {"watch", "unwatch"}:
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
                        manual_only=private
                        or bool(batch)
                        or purpose in {"topic", "transfer"},
                    )
                    if batch:
                        self.deliveries.finish_silent_batch(event.id, batch)
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
                """SELECT m.raw_json FROM coworker_native_transfers t
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
        if row is None:
            raise ValueError("topic transfer origin is unavailable")
        source_raw = _mapping(json.loads(str(row["raw_json"])))
        source_raw = self._enrich_message(
            source_raw, str(source_id), event.chat_id, str(bot)
        )
        message = MessageNormalizer(owner_open_id=str(bot)).normalize(
            source_raw, default_chat_type="group"
        )
        upsert = self.store.upsert_message_with_revision(message)
        if upsert.requires_confirmation or upsert.is_deleted:
            raise ValueError("topic transfer source cannot be verified")
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
        self.validate_workspace(session.agent_working_dir)
        if session.backend_session_id:
            raise ValueError("new topic already has a Codex session")
        if self.app_server is None:
            raise RuntimeError("Codex app-server is unavailable")
        prompt = (
            "飞书话题已经建立并确认接单。现在处理以下请求，按需使用原生工具和子 Agent；"
            "项目负责在本话题发布最终回复，不要使用工具自行发送或回复飞书消息。只使用群资料；明确记住时写入本群记忆 "
            + str(self.shared_memory(policy).path)
            + "，不要读取私聊笔记。返回 action=reply 和自然文本，或 action=silent；"
            "需要 owner 判断、授权或替其承诺时设置 approval_required=true 并写明 approval_reason。\n"
            + _code_block(
                json.dumps(_prompt_message(message), ensure_ascii=False), "json"
            )
        )
        parent = source_raw.get("parent_id") or source_raw.get("reply_to_message_id")
        if isinstance(parent, str) and parent:
            quote = self._quoted_message(parent)
            if quote is not None:
                prompt += "\n直接引用：\n" + _code_block(
                    json.dumps(quote, ensure_ascii=False), "json"
                )
        self.events.mark_agent_started(event.id)
        result = self.app_server.run_turn(
            prompt,
            session_id=None,
            cwd=session.agent_working_dir,
            output_model=NativeOutput,
            on_thread=lambda thread_id: self._register_native_root(
                thread_id, session.id, event.id, str(bot)
            ),
            private=True,
            unload_delay_seconds=1,
            dry_run_safety=self.execution_mode == "dry_run",
        )
        if not result.ok:
            raise RuntimeError(result.error or "topic transfer turn failed")
        self._check_current_policy(policy, event.chat_id, activation)
        output = NativeOutput.model_validate(result.json_data)
        if output.action == "silent":
            return
        if output.action != "reply":
            raise ValueError("topic transfer must produce a reply or remain silent")
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
                )
            return
        self._queue_native_reply(
            event_id=event.id,
            session_id=session.id,
            source_revision=upsert.revision,
            source_event_id=origin_id,
            source_message_id=str(source_id),
            target_message_id=str(target),
            text=composed.text,
            reply_in_thread=True,
        )

    def _watch_batch(
        self, event: CoworkerEvent, bot: str
    ) -> tuple[list[tuple[int, str, int]], list[dict[str, Any]]]:
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
                preliminary = MessageNormalizer(owner_open_id=bot).normalize(
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
                or (parent == event.message_id and _is_supplement(preliminary.text))
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
        for row, raw in candidates:
            enriched = self._enrich_message(
                raw, str(row["message_id"]), event.chat_id, bot
            )
            if (
                self._topic_for_message(enriched, str(row["message_id"]))
                != event.topic_key
            ):
                break
            message = MessageNormalizer(owner_open_id=bot).normalize(
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
        reserved = self.deliveries.reserve_watch_batch(event.id, batch)
        return batch[:reserved], messages[:reserved]

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
        self.validate_workspace(session.agent_working_dir)
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
        message = MessageNormalizer(owner_open_id=bot).normalize(
            raw, default_chat_type="p2p" if private else "group"
        )
        upsert = self.store.upsert_message_with_revision(message)
        if upsert.requires_confirmation or upsert.is_deleted:
            raise ValueError("subagent source revision cannot be verified")
        if private and (
            message.chat_type != "p2p" or message.sender_id != self.config.owner.open_id
        ):
            raise ValueError("private sender is not the owner")
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
            "子 Agent 已结束。查看结果并判断是否需要回复；项目发布最终答复，不要用工具发送飞书消息。"
            "根据当前原请求判断子 Agent 的结果是否仍然适用。"
            "返回 action=reply 和自然文本，或 action=silent；需要 owner 判断或授权时设置 approval_required=true。\n"
            + _code_block(json.dumps(notice, ensure_ascii=False), "json")
        )
        self.events.mark_agent_started(event.id)
        result = server.run_turn(
            prompt,
            session_id=session.backend_session_id,
            cwd=session.agent_working_dir,
            output_model=NativeOutput,
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
        output = NativeOutput.model_validate(result.json_data)
        if output.action == "silent":
            return
        if output.action != "reply":
            raise ValueError("a child completion cannot change a topic watch")
        composed = SendComposer(owner_open_id=self.config.owner.open_id).compose(
            proposed_reply=output.text,
            reply_target=None,
            chat_type="p2p" if private else "group",
        )
        if composed.had_forbidden_mentions or not composed.text:
            raise ValueError("reply is empty or contains an unauthorized mention")
        disabled_reason = self._disabled_reply_reason(event.chat_id, private=private)
        if output.approval_required or disabled_reason:
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
                    manual_only=private or reply_target != message.message_id,
                )
            return
        self._queue_native_reply(
            event_id=event.id,
            session_id=session.id,
            source_revision=upsert.revision,
            source_event_id=source_event_id,
            source_message_id=message.message_id,
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

    def _quoted_message(self, message_id: str) -> dict[str, Any] | None:
        try:
            page = self.feishu.get_messages(as_identity="bot", message_ids=[message_id])
        except Exception as exc:
            raise _SourceUnavailable("quoted message unavailable") from exc
        return next(
            (item for item in page.items if item.get("message_id") == message_id), None
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
    ) -> None:
        self._require_certain_steers(event.id, session_id)
        with self.store.connect() as conn:
            steered = conn.execute(
                "SELECT 1 FROM coworker_native_steers WHERE origin_event_id = ? AND session_id = ? AND status = 'accepted' LIMIT 1",
                (event.id, session_id),
            ).fetchone()
        manual_only = manual_only or steered is not None
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
            },
        )
        self.wake()

    def _deliver(self, delivery: NativeDelivery) -> None:
        sent_id: str | None = None
        topic_thread: str | None = None
        try:
            self._validate_delivery(delivery)
            for source_id in [
                delivery.source_message_id,
                *(item[0] for item in delivery.context_sources),
            ]:
                source = self.store.get_message(source_id)
                if source is None:
                    raise ValueError("reply source is unavailable")
                saved = _mapping(json.loads(str(source["raw_json"])))
                page = self.feishu.get_messages(
                    as_identity="bot", message_ids=[source_id]
                )
                current = next(
                    (
                        item
                        for item in page.items
                        if item.get("message_id") == source_id
                    ),
                    None,
                )
                if (
                    current is None
                    or current.get("chat_id") not in (None, delivery.chat_id)
                    or current.get("is_deleted")
                    or current.get("deleted")
                    or message_content_fingerprint(current)
                    != saved.get("coworker_source_content_sha256")
                    or (
                        saved.get("coworker_source_update_time") is not None
                        and current.get("update_time")
                        != saved.get("coworker_source_update_time")
                    )
                ):
                    raise ValueError("reply source cannot be verified")
            self._validate_delivery(delivery)
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
            if self.execution_mode == "dry_run":
                self.deliveries.finish(delivery.id, status="previewed")
                return
            self._validate_delivery(delivery)
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

    def _validate_delivery(self, delivery: NativeDelivery) -> Any:
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
            if self._group_activation_id(delivery.chat_id) != delivery.activation_id:
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
        source = self.store.get_message(delivery.source_message_id)
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
        for message_id, revision in delivery.context_sources:
            context = self.store.get_message(message_id)
            if (
                context is None
                or context["chat_id"] != delivery.chat_id
                or context["revision"] != revision
                or context["is_deleted"]
            ):
                raise ValueError("watch context revision changed")
        return source


class NativeCoworkerWorker(CoworkerWorker):
    service: NativeCoworkerService

    def start(self) -> None:
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
