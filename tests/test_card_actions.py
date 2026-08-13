from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from feishu_shadow_agent.approval_cards import (
    CARD_ACTION_PROTOCOL,
    build_approval_card,
)
from feishu_shadow_agent.card_actions import (
    CardActionProcessor,
    FeishuCardActionConnection,
    parse_card_action,
)
from feishu_shadow_agent.config import AppConfig, OwnerConfig
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _store_with_approval(tmp_path: Path) -> tuple[SQLiteStore, str]:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()
    with store.connect() as conn:
        task_id = int(
            conn.execute(
                """
                INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, created_at, updated_at, chat_type)
                VALUES ('t_card', 'watching', 'oc_1', 'om_root', 'label', 'now', 'now', 'p2p')
                """
            ).lastrowid
        )
    approval_pk = store.create_send_reply_approval(
        task_id=task_id,
        preview="suggested reply",
        payload={
            "reply_target_message_id": "om_root",
            "text": "suggested reply",
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
    action: str = "edit_send",
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
        "send_suggestion",
        "edit_send",
        "no_send_keep_watching",
        "no_send_end_task",
    }
    assert all(button["value"]["approval_id"] == "a_1" for button in buttons)
    assert all(button["action_type"] == "form_submit" for button in buttons)


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
        assert "需要填写回复内容" in str(exc)
    else:
        raise AssertionError("empty edited reply should be rejected")


class FakeChannel:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.connected = False
        self.disconnected = False

    def on(self, name: str, handler: Any) -> Any:
        self.handlers[name] = handler
        return lambda: None

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True


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
