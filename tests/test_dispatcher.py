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


def test_dry_run_preview_keeps_action_pending(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={"reply_target_message_id": "om_target", "text": "hello", "identity": "user"},
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


def test_actual_dispatch_dry_run_failure_marks_failed_without_send(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={"reply_target_message_id": "om_target", "text": "hello", "identity": "user"},
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


def test_actual_dispatch_dry_run_exception_marks_failed_without_stuck_sending(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={"reply_target_message_id": "om_target", "text": "hello", "identity": "user"},
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


def test_actual_dispatch_send_exception_marks_failed_without_stuck_sending(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={"reply_target_message_id": "om_target", "text": "hello", "identity": "user"},
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
    assert action.status == "failed"
    assert action.result["error_stage"] == "send"
    assert action.result["send"]["error"] == "send exploded"


def test_actual_dispatch_records_sent_id_and_associates_readback(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    watch_minutes_seen: list[int] = []
    monkeypatch.setattr(
        dispatcher_module,
        "_watch_until",
        lambda watch_minutes: watch_minutes_seen.append(watch_minutes) or "custom-watch-until",
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
    with store.connect() as conn:
        message = conn.execute("SELECT sender_role FROM messages WHERE message_id = ?", ("om_sent",)).fetchone()
        task_message = conn.execute(
            "SELECT role FROM task_messages WHERE task_id = ? AND message_id = ?",
            (task_id, "om_sent"),
        ).fetchone()
        task = conn.execute("SELECT watch_until FROM tasks WHERE id = ?", (task_id,)).fetchone()
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
        payload={"reply_target_message_id": "om_target", "text": "hello", "identity": "user"},
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


def test_actual_send_without_readback_still_marks_sent_with_warning(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={"reply_target_message_id": "om_target", "text": "hello", "identity": "user"},
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


def test_owner_notification_can_be_sent_independently(tmp_path: Path) -> None:
    store, dispatcher, fake = _dispatcher(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_owner_notification_action(
        task_id=task_id,
        payload={"type": "approval_required", "task_id": "t_1", "commands": ["/approve a_1"]},
    )
    fake.owner_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], 0, json_data={"data": {"message_id": "om_owner_sent"}}),
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


def test_owner_notification_actual_has_budget_beyond_blocked_send_reply_previews(tmp_path: Path) -> None:
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
        payload={"type": "approval_required", "task_id": "t_1", "commands": ["/approve a_1"]},
    )
    fake.owner_results.extend(
        [
            LarkCliResult(["dry"], 0, json_data={"api": []}),
            LarkCliResult(["send"], 0, json_data={"data": {"message_id": "om_owner_sent"}}),
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
