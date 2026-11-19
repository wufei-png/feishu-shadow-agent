from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from .agent_backend import AgentRunResult
from .config import AppConfig, ChatPolicyConfig, CoworkerConfig, OwnerConfig
from .coworker import CoworkerService, event_message_from_raw
from .coworker_store import GROUP_MAIN_KEY
from .jsonl import JSONLLogger
from .message_normalization import MessageNormalizer
from .store.policy_store import COWORKER_POLICY_KEY
from .store.schema import require_current_schema
from .store.sqlite_store import SQLiteStore
from .types import MessagePage


class CoworkerReplaySnapshot(BaseModel):
    """Recorded raw SDK events; their bodies may contain private group data."""

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    bot_open_id: str = Field(min_length=1)
    source_kind: Literal["raw_sdk", "reconstructed_local"] = "raw_sdk"
    chat_layout: Literal["group", "topic"] = "topic"
    trigger_mode: Literal["mention", "followups"] = "mention"
    history_source: Literal["live_on_demand", "local_cache"] = "live_on_demand"
    history_limit: int = Field(default=30, ge=1, le=50)
    events: list[dict[str, Any]] = Field(max_length=1000)


def snapshot_from_local_store(
    database: Path, *, app_id: str, bot_open_id: str, limit: int
) -> CoworkerReplaySnapshot:
    """Reconstruct an approximate fixture from the configured group's stored messages."""
    if not 1 <= limit <= 1000:
        raise ValueError("replay snapshot limit must be between 1 and 1000")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        require_current_schema(conn)
        row = conn.execute(
            "SELECT policy_json FROM product_policies WHERE key = ?",
            (COWORKER_POLICY_KEY,),
        ).fetchone()
        if row is None:
            raise ValueError("coworker policy is unavailable")
        policy = CoworkerConfig.model_validate_json(row["policy_json"])
        if not policy.enabled or not policy.chat_id:
            raise ValueError("coworker policy is not enabled")
        if (
            conn.execute(
                "SELECT 1 FROM chat_policies WHERE chat_id = ?", (policy.chat_id,)
            ).fetchone()
            is None
        ):
            raise ValueError("coworker group policy is unavailable")
        rows = conn.execute(
            """
            SELECT e.message_id, e.topic_key, m.sender_id, m.sender_type, m.sent_at,
                   m.thread_id, m.reply_to_message_id, m.direct_mention, m.at_all,
                   m.message_type, m.text, m.raw_json, m.normalized_json
            FROM coworker_events e JOIN messages m ON m.message_id = e.message_id
            WHERE e.app_id = ? AND e.chat_id = ? AND e.status = 'completed'
              AND m.is_deleted = 0 AND m.text IS NOT NULL
            ORDER BY e.id DESC LIMIT ?
            """,
            (app_id, policy.chat_id, limit),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for stored in reversed(rows):
        raw: dict[str, Any] = json.loads(stored["raw_json"])
        normalized: dict[str, Any] = json.loads(stored["normalized_json"])
        mentions: list[dict[str, Any]] = [
            {"id": {"open_id": value}}
            for value in normalized.get("mentions", [])
            if isinstance(value, str)
        ]
        if stored["direct_mention"] and not any(
            item["id"]["open_id"] == bot_open_id for item in mentions
        ):
            mentions.append({"id": {"open_id": bot_open_id}})
        if stored["at_all"]:
            mentions.append({"id": {"open_id": "all"}, "key": "@_all"})
        message = {
            "message_id": stored["message_id"],
            "chat_id": policy.chat_id,
            "chat_type": "group",
            "thread_id": None
            if stored["topic_key"] == GROUP_MAIN_KEY
            else raw.get("thread_id") or stored["thread_id"] or stored["topic_key"],
            "root_id": raw.get("root_id"),
            "parent_id": raw.get("parent_id") or stored["reply_to_message_id"],
            "message_type": stored["message_type"],
            "content": raw.get("content", {"text": stored["text"]}),
            "create_time": raw.get("create_time") or stored["sent_at"],
            "mentions": mentions,
        }
        events.append(
            {
                "header": {
                    "app_id": app_id,
                    "event_id": f"replay-{stored['message_id']}",
                },
                "event": {
                    "sender": {
                        "sender_id": {"open_id": stored["sender_id"]},
                        "sender_type": stored["sender_type"],
                    },
                    "message": message,
                },
            }
        )
    return CoworkerReplaySnapshot(
        app_id=app_id,
        chat_id=policy.chat_id,
        bot_open_id=bot_open_id,
        source_kind="reconstructed_local",
        chat_layout="group"
        if any(row["topic_key"] == GROUP_MAIN_KEY for row in rows)
        else "topic",
        trigger_mode=policy.trigger_mode,
        history_source=policy.history_source,
        history_limit=policy.history_limit,
        events=events,
    )


class _RecordedFeishu:
    def __init__(self, bot_open_id: str):
        self.bot_open_id = bot_open_id
        self.messages: list[dict[str, Any]] = []
        self.last_source_message_id: str | None = None

    def add(self, raw: dict[str, Any]) -> None:
        event = cast(dict[str, Any], raw["event"])
        self.messages.append(event_message_from_raw(event, bot_id=self.bot_open_id))

    def get_messages(self, *, as_identity: str, message_ids: list[str]) -> MessagePage:
        if as_identity != "bot":
            raise ValueError("offline replay requires bot identity")
        self.last_source_message_id = message_ids[0] if len(message_ids) == 1 else None
        normalizer = MessageNormalizer(owner_open_id=self.bot_open_id)
        return MessagePage(
            items=[
                message | {"content": normalizer.normalize(message).text}
                if message.get("message_type") == "text"
                else message
                for message in self.messages
                if message.get("message_id") in message_ids
            ]
        )

    def list_thread_messages(
        self,
        *,
        thread_id: str,
        page_size: int,
        as_identity: str,
        order: str,
        page_token: str | None = None,
    ) -> MessagePage:
        if as_identity != "bot" or order != "desc" or page_token is not None:
            raise ValueError("offline replay requires descending bot history")
        items = [
            message
            for message in self.messages
            if thread_id
            in (
                message.get("thread_id"),
                message.get("root_id"),
                message.get("message_id"),
            )
        ]
        return MessagePage(
            items=list(reversed(items))[:page_size], has_more=len(items) > page_size
        )

    def list_chat_messages(
        self,
        *,
        chat_id: str,
        start: str | None,
        end: str | None,
        page_size: int,
        as_identity: str,
        order: str,
        page_token: str | None = None,
    ) -> MessagePage:
        if as_identity != "bot" or order != "desc" or page_token is not None:
            raise ValueError("offline replay requires descending bot history")
        items = [item for item in self.messages if item.get("chat_id") == chat_id]
        return MessagePage(
            items=list(reversed(items))[:page_size], has_more=len(items) > page_size
        )


class _SilentBackend:
    def __init__(self, feishu: _RecordedFeishu, history_source: str) -> None:
        self.feishu = feishu
        self.history_source = history_source
        self.turns: list[dict[str, Any]] = []

    def structured_task_session(
        self,
        prompt: str,
        *,
        output_model: type[BaseModel],
        session_id: str | None,
        cwd: str | Path | None,
    ) -> AgentRunResult:
        del prompt, output_model, cwd
        if self.feishu.last_source_message_id is None:
            raise ValueError("offline replay did not fetch a source message")
        self.turns.append(
            {
                "resume": session_id is not None,
                "message_ids": [self.feishu.last_source_message_id],
                "history_source": self.history_source,
            }
        )
        return AgentRunResult(
            argv=[],
            exit_code=0,
            session_id=session_id or f"offline-{len(self.turns)}",
            json_data={"action": "silent", "text": "", "partner": None},
        )

    def natural_task_session(
        self, prompt: str, *, session_id: str | None, cwd: str | Path | None
    ) -> AgentRunResult:
        return self.structured_task_session(
            prompt, output_model=BaseModel, session_id=session_id, cwd=cwd
        )


def replay_recorded_events(snapshot: CoworkerReplaySnapshot) -> dict[str, Any]:
    """Rebuild a fresh local state with fake Feishu and a silent fake backend."""
    for raw in snapshot.events:
        header = (
            cast(dict[str, Any], raw.get("header"))
            if isinstance(raw.get("header"), dict)
            else {}
        )
        event = (
            cast(dict[str, Any], raw.get("event"))
            if isinstance(raw.get("event"), dict)
            else {}
        )
        message = (
            cast(dict[str, Any], event.get("message"))
            if isinstance(event.get("message"), dict)
            else {}
        )
        if (
            not header
            or header.get("app_id") not in (None, "", snapshot.app_id)
            or not message
            or message.get("chat_id") != snapshot.chat_id
            or message.get("chat_type") != "group"
        ):
            raise ValueError(
                "every recorded event must belong to the selected app and group"
            )
    with tempfile.TemporaryDirectory(prefix="coworker-replay-") as directory:
        root = Path(directory)
        policy = CoworkerConfig(
            enabled=True,
            chat_id=snapshot.chat_id,
            trigger_mode=snapshot.trigger_mode,
            history_source=snapshot.history_source,
            history_limit=snapshot.history_limit,
            memory_maintenance=False,
            send_mode="project",
        )
        config = AppConfig(
            owner=OwnerConfig(open_id="offline-replay-owner"),
            coworker=policy,
            chats={
                snapshot.chat_id: ChatPolicyConfig(auto_reply=True, bot_joined=True)
            },
        )
        store = SQLiteStore(root / "replay.sqlite3")
        store.import_product_policy_from_config(config)
        feishu = _RecordedFeishu(snapshot.bot_open_id)
        backend = _SilentBackend(feishu, snapshot.history_source)

        def backend_factory(
            _config: AppConfig, _policy: CoworkerConfig, _path: Path
        ) -> _SilentBackend:
            return backend

        service = CoworkerService(
            store=store,
            config=config,
            feishu=feishu,
            logger=JSONLLogger(root / "replay.jsonl"),
            base_dir=root,
            app_id=snapshot.app_id,
            dry_run=True,
            chat_layout=snapshot.chat_layout,
            backend_factory=backend_factory,
        )
        for raw in snapshot.events:
            feishu.add(raw)
            service.receive(raw, snapshot.bot_open_id)
            while service.process_next():
                pass
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT message_id, status FROM coworker_events ORDER BY id"
            ).fetchall()
        return {
            "offline": True,
            "backend": "silent_fake",
            "source_kind": snapshot.source_kind,
            "chat_layout": snapshot.chat_layout,
            "trigger_mode": snapshot.trigger_mode,
            "history_source": snapshot.history_source,
            "events": [dict(row) for row in rows],
            "turns": backend.turns,
            "limitations": [
                "only recorded events are visible",
                "history and provider sessions start empty",
                "model history queries, decisions, external tools, delivery and delegation are not replayed",
                "local-store snapshots reconstruct events and omit messages not stored locally",
            ],
        }
