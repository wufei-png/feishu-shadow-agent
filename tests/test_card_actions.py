from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    parse_card_action,
)
from feishu_shadow_agent.config import AppConfig, OwnerConfig
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


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
