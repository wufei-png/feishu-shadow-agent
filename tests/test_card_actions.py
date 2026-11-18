from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from feishu_shadow_agent.approval_cards import (
    CARD_ACTION_PROTOCOL,
    CARD_REPLY_MAX_LENGTH,
    build_approval_card,
    build_approval_result_card,
    card_form_reply_value,
)
from feishu_shadow_agent.card_actions import (
    CardActionProcessor,
    FeishuCardActionConnection,
    create_card_action_connection,
    parse_card_action,
)
from feishu_shadow_agent.config import AppConfig, OwnerConfig
from feishu_shadow_agent.daemon import Daemon
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.lark_channel_compat import create_channel
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult


def _store_with_approval(
    tmp_path: Path, *, text: str = "suggested reply"
) -> tuple[SQLiteStore, str]:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.initialize()
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, created_at, updated_at, chat_type)
            VALUES ('t_card', 'watching', 'oc_1', 'om_root', 'label', 'now', 'now', 'p2p')
            """
        )
        assert cursor.lastrowid is not None
        task_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO messages(message_id, chat_id, chat_type, raw_json, inserted_at) VALUES (?, ?, ?, ?, ?)",
            ("om_root", "oc_1", "p2p", "{}", "now"),
        )
    approval_pk = store.create_send_reply_approval(
        task_id=task_id,
        preview=text,
        payload={
            "reply_target_message_id": "om_root",
            "text": text,
            "identity": "user",
            "decision_reason": "commitment_or_authorization",
        },
        approval_timeout_hours=None,
    )
    with store.connect() as conn:
        approval_id = conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?", (approval_pk,)
        ).fetchone()["short_id"]
    return store, str(approval_id)


def _event(
    approval_id: str,
    *,
    action: str = "send",
    operator: str = "ou_owner",
    event_id: str = "evt_1",
    form: dict[str, Any] | None = None,
) -> Any:
    return SimpleNamespace(
        message_id="om_card",
        operator=SimpleNamespace(open_id=operator),
        action=SimpleNamespace(
            value={
                "protocol": CARD_ACTION_PROTOCOL,
                "action": action,
                "approval_id": approval_id,
            },
            form_value=form or {},
        ),
        raw={"header": {"event_id": event_id}},
    )


def test_approval_card_binds_concrete_approval_and_form_fields() -> None:
    card = build_approval_card(
        {
            "type": "approval_required",
            "task_id": "t_1",
            "approval_id": "a_1",
            "reason": "commitment_or_authorization",
            "incoming_message": {"text": "Can you commit to Friday?"},
            "suggested_reply": "Yes, by Friday.",
            "approvable": True,
        }
    )

    form = card["body"]["elements"][2]
    assert card["schema"] == "2.0"
    assert form["tag"] == "form"
    assert {item.get("name") for item in form["elements"]} >= {
        "final_reply",
        "feedback_reason",
        "note",
    }
    buttons = [column["elements"][0] for column in form["elements"][-1]["columns"]]
    assert {button["value"]["action"] for button in buttons} == {
        "send",
        "no_send_keep_watching",
        "no_send_end_task",
    }
    reply_input = next(
        item for item in form["elements"] if item.get("name") == "final_reply"
    )
    assert reply_input["label"]["content"] == "实际回复"
    assert reply_input["default_value"] == "Yes, by Friday."
    assert reply_input["required"] is False
    assert reply_input["max_length"] == CARD_REPLY_MAX_LENGTH
    assert all(button["value"]["approval_id"] == "a_1" for button in buttons)
    assert all(button["action_type"] == "form_submit" for button in buttons)


def test_approval_card_displays_destination_and_available_location() -> None:
    base = {
        "approval_id": "a_1",
        "task_id": "t_1",
        "suggested_reply": "hello",
        "reply_target_message_id": "om_root",
    }
    card = build_approval_card(
        base
        | {
            "reply_destination": {
                "chat_id": "oc_1",
                "location": "thread",
                "thread_id": "omt_1",
            }
        }
    )
    details = card["body"]["elements"][0]["text"]["content"]
    assert "回复目标：om_root" in details
    assert "回复位置：话题" in details

    unresolved = build_approval_card(
        base | {"approvable": False, "allowed_reply_locations": ["thread"]}
    )
    details = unresolved["body"]["elements"][0]["text"]["content"]
    assert "--location <thread>" in details


def test_unapprovable_card_shows_preview_without_enabling_send() -> None:
    card = build_approval_card(
        {
            "approval_id": "a_1",
            "task_id": "t_1",
            "suggested_reply": "",
            "preview": "候选回复",
            "approvable": False,
            "allowed_reply_locations": ["main", "thread"],
        }
    )
    details = card["body"]["elements"][0]["text"]["content"]
    assert "建议回复：候选回复" in details
    buttons = card["body"]["elements"][2]["elements"][-1]["columns"]
    assert all(column["elements"][0]["value"]["action"] != "send" for column in buttons)


def test_approval_card_keeps_long_suggestion_in_details_but_caps_form_default() -> None:
    suggested = "prefix " + ("字" * 1200)
    card = build_approval_card(
        {
            "type": "approval_required",
            "task_id": "t_1",
            "approval_id": "a_1",
            "reason": "commitment_or_authorization",
            "suggested_reply": suggested,
            "approvable": True,
        }
    )
    details = card["body"]["elements"][0]["text"]["content"]
    reply_input = next(
        item
        for item in card["body"]["elements"][2]["elements"]
        if item.get("name") == "final_reply"
    )

    assert suggested in details
    assert reply_input["default_value"] == card_form_reply_value(suggested)
    assert len(reply_input["default_value"]) == CARD_REPLY_MAX_LENGTH


def test_card_actions_wait_for_runtime_health_and_can_retry_after_recovery(
    tmp_path: Path,
) -> None:
    store, approval_id = _store_with_approval(tmp_path)
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    wakeups: list[str] = []

    class Health:
        ok = True

        def run_runtime_critical(self) -> list[HealthCheckResult]:
            return [
                HealthCheckResult(
                    "owner_config", "critical", "ok" if self.ok else "failed", "test"
                )
            ]

    health = Health()
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=health,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=False,
        runtime_health_interval_seconds=0,
    )
    connection = create_card_action_connection(
        store=store,
        config=config,
        logger=logger,
        wake=lambda: wakeups.append("wake"),
        execution_mode="production",
        runtime_allows_actions=daemon.runtime_health_allows_card_actions,
    )
    scheduled: list[str] = []

    class Channel:
        def schedule(self, coroutine: Any) -> None:
            scheduled.append("updated")
            coroutine.close()

    connection.processor.bind_channel(Channel())  # type: ignore[arg-type]
    event = _event(approval_id, action="send_suggestion")

    assert connection._runtime_allows_card_action() is False
    assert daemon._runtime_health_ok_for_tick(run_id="run_1") is True
    health.ok = False
    assert daemon._runtime_health_ok_for_tick(run_id="run_1") is False
    assert connection._runtime_allows_card_action() is False
    with store.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM approvals WHERE short_id = ?", (approval_id,)
            ).fetchone()["status"]
            == "pending"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM actions WHERE kind = 'send_reply'"
            ).fetchone()[0]
            == 0
        )
    assert scheduled == []
    assert wakeups == []

    health.ok = True
    assert daemon._runtime_health_ok_for_tick(run_id="run_1") is True
    assert connection._runtime_allows_card_action() is True
    assert connection.processor.handle(event).status == "applied"
    assert scheduled == ["updated"]
    assert wakeups == ["wake"]


def test_sdk_rejects_unhealthy_click_before_dedup_and_returns_toast(
    tmp_path: Path,
) -> None:
    pytest.importorskip("lark_channel")
    from lark_channel.channel.safety.pipeline import SafetyPipeline
    from lark_channel.core.json import JSON as SDKJSON

    async def run() -> None:
        store, approval_id = _store_with_approval(tmp_path)
        processor = CardActionProcessor(
            store=store,
            config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
            logger=JSONLLogger(tmp_path / "agent.jsonl"),
            wake=lambda: None,
            execution_mode="production",
        )
        channel = create_channel("cli_test", "secret_test")
        sdk: Any = channel._channel
        handled = asyncio.Event()
        outcomes: list[str] = []
        scheduled: list[asyncio.Task[Any]] = []
        healthy = False

        async def ignore_message(_message: Any) -> None:
            return None

        def schedule(coroutine: Any) -> asyncio.Task[Any]:
            task = asyncio.create_task(coroutine)
            scheduled.append(task)
            return task

        def handle(event: Any) -> None:
            outcomes.append(processor.handle(event).status)
            handled.set()

        def event(event_id: str) -> bytes:
            return json.dumps(
                {
                    "schema": "2.0",
                    "header": {
                        "event_id": event_id,
                        "event_type": "card.action.trigger",
                    },
                    "event": {
                        "operator": {"open_id": "ou_owner"},
                        "context": {
                            "open_message_id": "om_card",
                            "open_chat_id": "oc_1",
                        },
                        "action": {
                            "tag": "button",
                            "value": {
                                "protocol": CARD_ACTION_PROTOCOL,
                                "action": "send_suggestion",
                                "approval_id": approval_id,
                            },
                            "form_value": {},
                        },
                    },
                }
            ).encode()

        sdk._safety = SafetyPipeline(
            loop=asyncio.get_running_loop(), on_message=ignore_message
        )
        sdk.schedule = schedule
        channel.on("cardAction", handle)
        channel.set_card_action_guard(lambda: healthy, blocked_message="retry later")
        dispatcher = sdk._build_dispatcher()
        try:
            rejected = dispatcher._do_without_validation(event("evt_blocked"))
            assert rejected.toast.type == "error"
            assert rejected.toast.content == "retry later"
            serialized = SDKJSON.marshal(rejected)
            assert serialized is not None
            assert json.loads(serialized)["toast"]["content"] == "retry later"
            assert scheduled == []
            with store.connect() as conn:
                assert (
                    conn.execute(
                        "SELECT status FROM approvals WHERE short_id = ?",
                        (approval_id,),
                    ).fetchone()["status"]
                    == "pending"
                )

            healthy = True
            dispatcher._do_without_validation(event("evt_retry"))
            await asyncio.wait_for(handled.wait(), timeout=2)
            assert outcomes == ["applied"]
            await asyncio.sleep(0)
            dispatcher._do_without_validation(event("evt_retry"))
            await asyncio.gather(*scheduled)
            await asyncio.sleep(0)
            assert outcomes == ["applied"]
        finally:
            await channel.disconnect()

    asyncio.run(run())


def test_card_action_only_processes_current_business_mode(tmp_path: Path) -> None:
    store, approval_id = _store_with_approval(tmp_path)
    common = {
        "store": store,
        "config": AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        "logger": JSONLLogger(tmp_path / "agent.jsonl"),
        "wake": lambda: None,
        "execution_mode": "production",
    }
    other_mode = CardActionProcessor(**common, approval_scope="coworker")  # type: ignore[arg-type]
    shadow = CardActionProcessor(**common, approval_scope="shadow")  # type: ignore[arg-type]
    event = _event(approval_id, action="send_suggestion")
    rejected = other_mode.handle(event)
    assert rejected.toast_type == "error"
    with store.connect() as conn:
        assert conn.execute("SELECT status FROM approvals").fetchone()[0] == "pending"
        assert conn.execute("SELECT COUNT(*) FROM approval_commands").fetchone()[0] == 0
    assert shadow.handle(event).status == "applied"


def test_card_action_is_owner_only_idempotent_and_wakes_dispatch(
    tmp_path: Path,
) -> None:
    store, approval_id = _store_with_approval(tmp_path)
    wakeups: list[str] = []
    processor = CardActionProcessor(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        wake=lambda: wakeups.append("wake"),
        execution_mode="production",
    )

    forbidden = processor.handle(
        _event(
            approval_id,
            operator="ou_intruder",
            form={"final_reply": "tampered"},
        )
    )
    first = processor.handle(
        _event(
            approval_id,
            form={"final_reply": "owner edited", "feedback_reason": "tone_or_style"},
        )
    )
    duplicate = processor.handle(
        _event(
            approval_id,
            form={"final_reply": "owner edited", "feedback_reason": "tone_or_style"},
        )
    )

    with store.connect() as conn:
        feedback = conn.execute("SELECT * FROM approval_feedback").fetchall()
        action = conn.execute(
            "SELECT execution_mode, payload_json FROM actions WHERE kind = 'send_reply'"
        ).fetchone()
    assert forbidden.status == "forbidden"
    assert first.status == "applied"
    assert first.toast == "操作已进入队列。"
    assert duplicate.status == "no_change"
    assert len(wakeups) == 2
    assert len(feedback) == 1
    assert feedback[0]["outcome"] == "edited_sent"
    assert feedback[0]["feedback_reason"] == "tone_or_style"
    assert action["execution_mode"] == "production"


def test_card_action_parser_requires_final_reply_and_uses_event_id() -> None:
    event = _event("a_1", form={"final_reply": " edited ", "note": " note "})
    request = parse_card_action(event)

    assert request.event_id == "evt_1"
    assert request.final_reply == "edited"
    assert request.note == "note"

    invalid = _event("a_1", form={})
    try:
        parse_card_action(invalid)
    except ValueError as exc:
        assert "发送需要填写回复内容" in str(exc)
    else:
        raise AssertionError("empty edited reply should be rejected")

    keep = parse_card_action(_event("a_1", action="no_send_keep_watching", form={}))
    end = parse_card_action(_event("a_1", action="no_send_end_task", form={}))
    assert keep.final_reply is None
    assert end.final_reply is None


def test_card_action_parser_keeps_legacy_send_actions_compatible() -> None:
    suggestion = parse_card_action(_event("a_1", action="send_suggestion", form={}))
    edited = parse_card_action(
        _event("a_1", action="edit_send", form={"final_reply": "reply"})
    )

    assert suggestion.action == "send_suggestion"
    assert edited.action == "edit_send"


def test_result_card_has_no_action_controls() -> None:
    card = build_approval_result_card(
        outcome="edited_sent", final_reply="updated reply"
    )

    assert card["header"]["title"]["content"] == "回复已发送"
    assert all(element.get("tag") != "form" for element in card["body"]["elements"])
    assert card["body"]["elements"][-1]["text"]["content"] == (
        "实际回复：\nupdated reply"
    )


class FakeChannel:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.connected = False
        self.disconnected = False
        self.updated_cards: list[tuple[str, dict[str, Any]]] = []

    def on(self, name: str, handler: Any) -> Any:
        self.handlers[name] = handler
        return lambda: None

    def set_card_action_guard(self, guard: Any, *, blocked_message: str) -> None:
        self.guard = guard
        self.blocked_message = blocked_message

    async def connect_until_ready(
        self,
        *,
        timeout: float | None = 30.0,  # noqa: ASYNC109
    ) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def update_card(self, message_id: str, card: dict[str, Any]) -> Any:
        self.updated_cards.append((message_id, card))
        return SimpleNamespace(success=True)

    def schedule(self, coro: Any) -> Any:
        return asyncio.run(coro)


def test_unified_send_updates_original_card_after_persisting_result(
    tmp_path: Path,
) -> None:
    store, approval_id = _store_with_approval(tmp_path)
    channel = FakeChannel()
    processor = CardActionProcessor(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        wake=lambda: None,
        execution_mode="production",
    )
    processor.bind_channel(channel)

    result = processor.handle(
        _event(approval_id, form={"final_reply": "suggested reply"})
    )

    assert result.status == "applied"
    assert len(channel.updated_cards) == 1
    message_id, card = channel.updated_cards[0]
    assert message_id == "om_card"
    assert card["header"]["title"]["content"] == "回复已发送"
    assert all(item.get("tag") != "form" for item in card["body"]["elements"])
    with store.connect() as conn:
        assert (
            conn.execute("SELECT outcome FROM approval_feedback").fetchone()["outcome"]
            == "suggestion_sent"
        )


def test_unchanged_truncated_form_send_uses_full_stored_suggestion(
    tmp_path: Path,
) -> None:
    suggested = "full suggestion " + ("字" * 1200)
    store, approval_id = _store_with_approval(tmp_path, text=suggested)
    displayed = card_form_reply_value(suggested)
    assert displayed != suggested
    processor = CardActionProcessor(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        wake=lambda: None,
        execution_mode="production",
    )

    result = processor.handle(_event(approval_id, form={"final_reply": displayed}))

    assert result.status == "applied"
    with store.connect() as conn:
        feedback = conn.execute(
            "SELECT outcome, final_reply FROM approval_feedback"
        ).fetchone()
        action = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'send_reply'"
        ).fetchone()
    payload = json.loads(action["payload_json"])
    assert feedback["outcome"] == "suggestion_sent"
    assert feedback["final_reply"] == suggested
    assert payload["text"] == suggested


def test_no_send_accepts_empty_reply_and_keeps_watching(tmp_path: Path) -> None:
    store, approval_id = _store_with_approval(tmp_path)
    processor = CardActionProcessor(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        wake=lambda: None,
        execution_mode="production",
    )

    result = processor.handle(
        _event(approval_id, action="no_send_keep_watching", form={})
    )

    assert result.status == "applied"
    with store.connect() as conn:
        feedback = conn.execute("SELECT outcome FROM approval_feedback").fetchone()
        task = conn.execute("SELECT status FROM tasks").fetchone()
    assert feedback["outcome"] == "no_send_keep_watching"
    assert task["status"] == "watching"


def test_card_connection_tracks_health_and_disconnects(tmp_path: Path) -> None:
    store, _ = _store_with_approval(tmp_path)
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    processor = CardActionProcessor(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        logger=logger,
        wake=lambda: None,
        execution_mode="dry_run",
    )
    channel = FakeChannel()
    connection = FeishuCardActionConnection(
        processor=processor,
        app_id="cli_test",
        app_secret="secret",
        startup_timeout_seconds=1,
        logger=logger,
        channel_factory=lambda _app_id, _app_secret: channel,
    )

    assert connection.start() is True
    assert connection.snapshot() == {"status": "healthy", "last_error": None}
    assert "cardAction" in channel.handlers
    connection.stop()
    assert channel.disconnected is True
    assert connection.snapshot()["status"] == "stopped"


def test_card_connection_missing_credentials_falls_back_without_factory_call(
    tmp_path: Path,
) -> None:
    store, _ = _store_with_approval(tmp_path)
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    processor = CardActionProcessor(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        logger=logger,
        wake=lambda: None,
        execution_mode="production",
    )
    calls: list[str] = []
    connection = FeishuCardActionConnection(
        processor=processor,
        app_id="",
        app_secret="",
        startup_timeout_seconds=1,
        logger=logger,
        channel_factory=lambda _a, _s: calls.append("called"),  # type: ignore[arg-type,return-value]
    )

    assert connection.start() is False
    assert connection.snapshot()["status"] == "unhealthy"
    assert calls == []
