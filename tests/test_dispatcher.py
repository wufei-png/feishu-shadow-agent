from __future__ import annotations

from pathlib import Path
from typing import Any

from feishu_shadow_agent import dispatcher as dispatcher_module
from feishu_shadow_agent.config import AppConfig, LifecycleConfig, OwnerConfig
from feishu_shadow_agent.dispatcher import Dispatcher
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import LarkCliResult, MessagePage


class FakeFeishu:
    def __init__(self):
        self.reply_results: list[LarkCliResult] = []
        self.owner_results: list[LarkCliResult] = []
        self.readback_pages: list[MessagePage] = []
        self.reply_calls: list[dict[str, Any]] = []
        self.owner_calls: list[dict[str, Any]] = []
        self.owner_card_calls: list[dict[str, Any]] = []
        self.mget_calls: list[dict[str, Any]] = []
        self.raise_reply_dry_run = False
        self.raise_reply_send = False

    def reply_message(self, **kwargs: Any) -> LarkCliResult:
        self.reply_calls.append(kwargs)
        if kwargs.get("dry_run") and self.raise_reply_dry_run:
            raise RuntimeError("dry-run exploded")
        if not kwargs.get("dry_run") and self.raise_reply_send:
            raise RuntimeError("send exploded")
        if self.reply_results:
            return self.reply_results.pop(0)
        return LarkCliResult(["lark-cli", "im", "+messages-reply"], 0, json_data={})

    def owner_message(self, **kwargs: Any) -> LarkCliResult:
        self.owner_calls.append(kwargs)
        if self.owner_results:
            return self.owner_results.pop(0)
        return LarkCliResult(["lark-cli", "im", "+messages-send"], 0, json_data={})

    def owner_card(self, **kwargs: Any) -> LarkCliResult:
        self.owner_card_calls.append(kwargs)
        if self.owner_results:
            return self.owner_results.pop(0)
        return LarkCliResult(["lark-cli", "im", "+messages-send"], 0, json_data={})

    def get_messages(self, **kwargs: Any) -> MessagePage:
        self.mget_calls.append(kwargs)
        if self.readback_pages:
            return self.readback_pages.pop(0)
        return MessagePage([])


def _config(**kwargs: Any) -> AppConfig:
    return AppConfig(owner=OwnerConfig(open_id="ou_owner", name="Owner"), **kwargs)


def _dispatcher(
    tmp_path: Path,
    fake: FakeFeishu | None = None,
    config: AppConfig | None = None,
    *,
    interactive_cards_available: bool = False,
) -> tuple[SQLiteStore, Dispatcher, FakeFeishu]:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    client = fake or FakeFeishu()
    return (
        store,
        Dispatcher(
            store=store,
            feishu_client=client,  # type: ignore[arg-type]
            config=config or _config(),
            logger=JSONLLogger(tmp_path / "agent.jsonl"),
            interactive_cards_available=interactive_cards_available,
        ),
        client,
    )


def _insert_task(store: SQLiteStore) -> int:
    store.migrate()
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, created_at, updated_at, chat_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("t_1", "watching", "oc_1", "om_target", "label", "now", "now", "group"),
        )
    return int(cursor.lastrowid)


def _attempts(store: SQLiteStore, action_id: int):
    return store.list_dispatch_attempts(action_id)


def test_dry_run_preview_keeps_action_pending(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.reply_results.append(LarkCliResult(["dry"], 0, json_data={"api": []}))

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=False,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert summary.previewed == 1
    assert action is not None
    assert action.status == "pending"
    assert action.result["dry_run"]["ok"] is True
    assert [call["dry_run"] for call in fake.reply_calls] == [True]


def test_dry_run_provenance_cannot_be_promoted_by_production_dispatch(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "preview only",
            "identity": "user",
        },
        execution_mode="dry_run",
    )
    assert action_id is not None

    summary = dispatcher.dispatch(
        run_id="run_prod",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=True,
    )

    action = store.get_action(action_id)
    assert summary.previewed == 1
    assert summary.sent == 0
    assert [call["dry_run"] for call in fake.reply_calls] == [True]
    assert action is not None
    assert action.status == "pending"
    assert action.execution_mode == "dry_run"
    assert action.result["blocked_actual_reason"] == "execution_mode_dry_run"


def test_actual_dispatch_dry_run_failure_marks_failed_without_send(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.reply_results.append(LarkCliResult(["dry"], 1, error="bad dry-run"))

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert summary.failed == 1
    assert action is not None
    assert action.status == "failed"
    assert action.result["error_stage"] == "dry_run"
    assert len(fake.reply_calls) == 1
    attempts = _attempts(store, action_id)
    assert len(attempts) == 1
    assert attempts[0].status == "failed"
    assert attempts[0].error_stage == "dry_run"
    assert attempts[0].dry_run_result["ok"] is False


def test_actual_dispatch_dry_run_exception_marks_failed_without_stuck_sending(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.raise_reply_dry_run = True

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert summary.failed == 1
    assert action is not None
    assert action.status == "failed"
    assert action.result["error_stage"] == "dry_run"
    assert action.result["dry_run"]["error"] == "dry-run exploded"
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "failed"
    assert attempts[0].error_stage == "dry_run"


def test_actual_dispatch_send_rejection_marks_failed_without_stuck_sending(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.reply_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], 1, error="send rejected"),
        ]
    )

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert summary.failed == 1
    assert action is not None
    assert action.status == "failed"
    assert action.result["error_stage"] == "send"
    assert action.result["send"]["error"] == "send rejected"
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "failed"
    assert attempts[0].error_stage == "send"


def test_actual_dispatch_transient_nonzero_send_failure_needs_review_and_is_not_auto_revived(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    payload = {
        "reply_target_message_id": "om_target",
        "text": "hello",
        "identity": "user",
    }
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload=payload,
    )
    assert action_id is not None
    fake.reply_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], 1, error="connection reset by peer"),
        ]
    )

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert summary.failed == 1
    assert action is not None
    assert action.status == "failed_needs_review"
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "uncertain"
    assert attempts[0].error_stage == "send"

    revived = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload=payload,
    )
    different_payload = {
        "reply_target_message_id": "om_target",
        "text": "different",
        "identity": "user",
    }
    different_blocked = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload=different_payload,
    )

    assert revived is None
    assert different_blocked is None
    assert store.get_action(action_id).status == "failed_needs_review"  # type: ignore[union-attr]
    store.cancel_dispatch_action(action_id)
    after_cancel = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload=different_payload,
    )
    assert after_cancel is not None


def test_actual_dispatch_send_exception_after_boundary_needs_review(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.reply_results.append(LarkCliResult(["dry"], 0, json_data={"api": []}))
    fake.raise_reply_send = True

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert summary.failed == 1
    assert action is not None
    assert action.status == "failed_needs_review"
    assert action.result["error_stage"] == "send"
    assert action.result["send"]["error"] == "send exploded"
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "uncertain"
    assert attempts[0].error_stage == "send"


def test_actual_dispatch_send_timeout_needs_review(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.reply_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], None, error="command timed out", timed_out=True),
        ]
    )

    dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert action is not None
    assert action.status == "failed_needs_review"
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "uncertain"
    assert attempts[0].send_result["timed_out"] is True


def test_actual_dispatch_missing_sent_message_id_needs_review(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.reply_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], 0, json_data={"data": {}}),
        ]
    )

    dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert action is not None
    assert action.status == "failed_needs_review"
    assert "sent_message_id_missing" in action.result["warnings"]
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "uncertain"


def test_actual_dispatch_records_sent_id_and_associates_readback(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    watch_minutes_seen: list[int] = []
    monkeypatch.setattr(
        dispatcher_module,
        "_watch_until",
        lambda watch_minutes: (
            watch_minutes_seen.append(watch_minutes) or "custom-watch-until"
        ),
    )
    store, dispatcher, fake = _dispatcher(
        tmp_path,
        config=_config(lifecycle=LifecycleConfig(watch_minutes=5)),
    )
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": '<at user_id="ou_a">Alice</at> hello',
            "identity": "bot",
        },
    )
    assert action_id is not None
    fake.reply_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], 0, json_data={"data": {"message_id": "om_sent"}}),
        ]
    )
    fake.readback_pages.append(
        MessagePage(
            [
                {
                    "message_id": "om_sent",
                    "chat_id": "oc_1",
                    "chat_type": "group",
                    "sender_id": "ou_bot",
                    "sender_type": "bot",
                    "create_time": "2026-06-22T10:00:00+08:00",
                    "reply_to_message_id": "om_target",
                    "content": {"text": "hello", "mentions": [{"open_id": "ou_a"}]},
                }
            ],
            raw={"ok": True},
        )
    )

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert summary.sent == 1
    assert action is not None
    assert action.status == "sent"
    assert action.result["sent_message_id"] == "om_sent"
    assert action.result["warnings"] == []
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "readback_ok"
    assert attempts[0].sent_message_id == "om_sent"
    assert attempts[0].send_result["ok"] is True
    assert attempts[0].readback_result["ok"] is True
    with store.connect() as conn:
        message = conn.execute(
            "SELECT sender_role FROM messages WHERE message_id = ?", ("om_sent",)
        ).fetchone()
        task_message = conn.execute(
            "SELECT role FROM task_messages WHERE task_id = ? AND message_id = ?",
            (task_id, "om_sent"),
        ).fetchone()
        task = conn.execute(
            "SELECT watch_until FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert message["sender_role"] == "agent_message"
    assert task_message["role"] == "agent_reply"
    assert task["watch_until"] == "custom-watch-until"
    assert watch_minutes_seen == [5]
    assert [call["dry_run"] for call in fake.reply_calls] == [True, False]
    assert fake.mget_calls == [{"as_identity": "bot", "message_ids": ["om_sent"]}]


def test_readback_exception_after_send_marks_sent_with_warning(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.reply_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], 0, json_data={"data": {"message_id": "om_sent"}}),
        ]
    )
    fake.readback_pages.append(MessagePage([{"content": {"text": "missing id"}}]))

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert summary.sent == 1
    assert action is not None
    assert action.status == "sent"
    assert action.result["sent_message_id"] == "om_sent"
    assert "readback_exception" in action.result["warnings"]
    assert action.result["readback"]["message_id"] == "om_sent"
    assert action.result["readback"]["exception_type"] == "ValueError"
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "send_ok"
    assert attempts[0].error_stage == "readback"


def test_readback_target_mismatch_marks_attempt_send_ok_with_warning(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.reply_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], 0, json_data={"data": {"message_id": "om_sent"}}),
        ]
    )
    fake.readback_pages.append(
        MessagePage(
            [
                {
                    "message_id": "om_sent",
                    "chat_id": "oc_1",
                    "chat_type": "group",
                    "sender_id": "ou_bot",
                    "sender_type": "bot",
                    "create_time": "2026-06-22T10:00:00+08:00",
                    "reply_to_message_id": "om_other",
                    "content": {"text": "hello"},
                }
            ]
        )
    )

    dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    attempts = _attempts(store, action_id)
    assert action is not None
    assert action.status == "sent"
    assert "readback_reply_target_mismatch" in action.result["warnings"]
    assert attempts[0].status == "send_ok"
    assert attempts[0].error_stage == "readback"


def test_actual_send_without_readback_still_marks_sent_with_warning(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    fake.reply_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], 0, json_data={"data": {"message_id": "om_sent"}}),
        ]
    )
    fake.readback_pages.append(MessagePage([]))

    dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert action is not None
    assert action.status == "sent"
    assert "readback_message_missing" in action.result["warnings"]
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "send_ok"
    assert attempts[0].finished_at is not None


def test_owner_notification_can_be_sent_independently(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_owner_notification_action(
        task_id=task_id,
        payload={
            "type": "approval_required",
            "task_id": "t_1",
            "approval_id": "a_1",
            "reason": "needs_owner",
            "source": {
                "chat_id": "oc_1",
                "chat_type": "group",
                "sender_name": "Ext",
                "task_label": "classification service",
            },
            "incoming_message": {
                "message_id": "om_target",
                "text": "classification service failed to start",
                "message_app_link": "https://applink.feishu.cn/client/chat/open?openChatId=oc_1&position=1",
            },
            "stage": "task_session",
            "reply_target_message_id": "om_root",
            "attempt_count": 3,
            "pending_approval_ids": ["a_1", "a_2"],
            "pending_approvals": [
                {
                    "approval_id": "a_1",
                    "kind": "send_reply",
                    "reason": "needs_owner",
                    "preview": "first pending reply",
                    "commands": [
                        "/approve a_1",
                        "/send t_1 <final reply>",
                        "/reject a_1",
                    ],
                }
            ],
            "statuses": {"missing_file": 1},
            "error": "session exploded",
            "suggested_reply": "",
            "approvable": False,
            "commands": ["/send t_1 <final reply>", "/reject a_1"],
        },
    )
    fake.owner_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(
                ["send"], 0, json_data={"data": {"message_id": "om_owner_sent"}}
            ),
        ]
    )
    fake.readback_pages.append(
        MessagePage(
            [
                {
                    "message_id": "om_owner_sent",
                    "chat_id": "oc_owner",
                    "chat_type": "p2p",
                    "sender_id": "ou_bot",
                    "sender_type": "bot",
                    "create_time": "2026-06-22T10:00:00+08:00",
                    "content": {"text": "notify"},
                }
            ],
            raw={"ok": True},
        )
    )

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=False,
        allow_owner_notification_actual=True,
    )

    action = store.get_action(action_id)
    assert summary.sent == 1
    assert action is not None
    assert action.status == "sent"
    assert action.result["sent_message_id"] == "om_owner_sent"
    assert [call["dry_run"] for call in fake.owner_calls] == [True, False]
    sent_text = fake.owner_calls[0]["text"]
    assert "reason: needs_owner" in sent_text
    assert "source: group oc_1 / Ext / classification service" in sent_text
    assert "message_id: om_target" in sent_text
    assert "incoming: classification service failed to start" in sent_text
    assert "message_link: https://applink.feishu.cn/client/chat/open?" in sent_text
    assert "suggested_reply: <none>" in sent_text
    assert "approvable: no" in sent_text
    assert "stage: task_session" in sent_text
    assert "reply_target_message_id: om_root" in sent_text
    assert "attempt_count: 3" in sent_text
    assert "pending_approval_ids: a_1, a_2" in sent_text
    assert "pending_approvals:" in sent_text
    assert (
        "- a_1 | send_reply | reason: needs_owner | preview: first pending reply"
        in sent_text
    )
    assert "commands: /approve a_1, /send t_1 <final reply>, /reject a_1" in sent_text
    assert 'statuses: {"missing_file": 1}' in sent_text
    assert "error: session exploded" in sent_text
    assert "/send t_1 <final reply>" in sent_text
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "readback_ok"


def test_approval_notification_uses_card_only_when_callback_is_healthy(
    tmp_path: Path,
) -> None:
    fake = FakeFeishu()
    store, dispatcher, _ = _dispatcher(tmp_path, fake, interactive_cards_available=True)
    task_id = _insert_task(store)
    store.create_owner_notification_action(
        task_id=task_id,
        payload={
            "type": "approval_required",
            "task_id": "t_1",
            "approval_id": "a_1",
            "reason": "commitment_or_authorization",
            "suggested_reply": "I will finish Friday.",
            "approvable": True,
        },
    )
    fake.owner_results.append(LarkCliResult(["dry"], 0, json_data={"api": []}))

    summary = dispatcher.dispatch(
        run_id="run_card",
        allow_send_reply_actual=False,
        allow_owner_notification_actual=False,
    )

    assert summary.previewed == 1
    assert fake.owner_calls == []
    assert len(fake.owner_card_calls) == 1
    assert fake.owner_card_calls[0]["card"]["schema"] == "2.0"
    assert fake.owner_card_calls[0]["dry_run"] is True


def test_owner_notification_neutralizes_freeform_mentions(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_owner_notification_action(
        task_id=task_id,
        payload={
            "type": "approval_required",
            "task_id": "t_1",
            "approval_id": "a_1",
            "reason": 'needs_owner <at user_id="ou_x">owner</at> @所有人 @ALL',
            "source": {
                "chat_id": "oc_1",
                "chat_type": "group",
                "sender_name": 'Mallory <at user_id="ou_x">owner</at> @All',
                "task_label": "classification @all",
            },
            "incoming_message": {
                "message_id": "om_target",
                "text": 'please notify <at user_id="ou_all">all</at> @_ALL',
            },
            "pending_approvals": [
                {
                    "approval_id": "a_1",
                    "kind": "send_reply",
                    "reason": '<at user_id="ou_x">owner</at>',
                    "preview": "reply @所有人 @_All",
                    "commands": [
                        "/approve a_1",
                        "/send t_1 <final reply>",
                        "/reject a_1",
                    ],
                }
            ],
            "suggested_reply": 'done <at user_id="ou_x">owner</at> @all',
            "preview": 'preview <at user_id="ou_x">owner</at> @_all @ALL',
            "commands": ["/send t_1 <final reply>", "/reject a_1"],
        },
    )
    fake.owner_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(
                ["send"], 0, json_data={"data": {"message_id": "om_owner_sent"}}
            ),
        ]
    )
    fake.readback_pages.append(MessagePage([]))

    dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=False,
        allow_owner_notification_actual=True,
    )

    action = store.get_action(action_id)
    assert action is not None
    assert action.status == "sent"
    sent_text = fake.owner_calls[0]["text"]
    assert "<at" not in sent_text
    assert "</at>" not in sent_text
    assert "@所有人" not in sent_text
    assert "@all" not in sent_text.lower()
    assert "@_all" not in sent_text.lower()
    assert "＠所有人" in sent_text
    assert "＠all" in sent_text
    assert "＠ALL" in sent_text
    assert "＠All" in sent_text
    assert "＠_ALL" in sent_text
    assert "＠_All" in sent_text
    assert "/send t_1 <final reply>" in sent_text


def test_stale_sending_is_marked_failed_needs_review_without_resend(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    assert action_id is not None
    claim = store.claim_action_for_dispatch(action_id, run_id="run_stale")
    assert claim is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE actions SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", action_id),
        )

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
    )

    action = store.get_action(action_id)
    assert summary.processed == 0
    assert fake.reply_calls == []
    assert action is not None
    assert action.status == "failed_needs_review"
    assert action.result["recovery_reason"] == "stale_sending_uncertain"
    attempts = _attempts(store, action_id)
    assert attempts[0].status == "uncertain"
    assert attempts[0].error_stage == "recovery"


def test_owner_notification_actual_has_budget_beyond_blocked_send_reply_previews(
    tmp_path: Path,
) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    for index in range(50):
        action_id = store.create_send_reply_action(
            task_id=task_id,
            target_message_id=f"om_target_{index}",
            payload={
                "reply_target_message_id": f"om_target_{index}",
                "text": f"reply {index}",
                "identity": "user",
                "source": "auto_reply",
            },
        )
        assert action_id is not None
    owner_action_id = store.create_owner_notification_action(
        task_id=task_id,
        payload={
            "type": "approval_required",
            "task_id": "t_1",
            "commands": ["/approve a_1"],
        },
    )
    fake.owner_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(
                ["send"], 0, json_data={"data": {"message_id": "om_owner_sent"}}
            ),
        ]
    )
    fake.readback_pages.append(
        MessagePage(
            [
                {
                    "message_id": "om_owner_sent",
                    "chat_id": "oc_owner",
                    "chat_type": "p2p",
                    "sender_id": "ou_bot",
                    "sender_type": "bot",
                    "create_time": "2026-06-22T10:00:00+08:00",
                    "content": {"text": "notify"},
                }
            ]
        )
    )

    summary = dispatcher.dispatch(
        run_id="run_1",
        allow_send_reply_actual=False,
        allow_owner_notification_actual=True,
        blocked_send_reply_reason="approval_inbox_failed",
    )

    owner_action = store.get_action(owner_action_id)
    assert summary.processed == 51
    assert summary.previewed == 50
    assert summary.sent == 1
    assert owner_action is not None
    assert owner_action.status == "sent"
    assert [call["dry_run"] for call in fake.reply_calls] == [True] * 50
    assert [call["dry_run"] for call in fake.owner_calls] == [True, False]
