from __future__ import annotations

from pathlib import Path
from typing import Any

from feishu_shadow_agent.config import AppConfig, ChatPolicyConfig, OwnerConfig
from feishu_shadow_agent.daemon import Daemon
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.processing import TaskProcessingService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult, HermesCliResult, LarkCliResult, MessagePage


class FakeHealthSuite:
    def __init__(self, results: list[HealthCheckResult]):
        self.results = results
        self.run_id = "run_1"
        self.calls = 0

    def run(self, *, send_test: bool = False) -> list[HealthCheckResult]:
        self.calls += 1
        return self.results

    def run_runtime_critical(self) -> list[HealthCheckResult]:
        self.calls += 1
        return self.results


class FakeFeishu:
    def __init__(self):
        self.fail_approval_inbox = False
        self.reply_calls: list[dict[str, Any]] = []
        self.owner_calls: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.search_items: dict[tuple[str, bool], list[dict[str, Any]]] = {}
        self.sent_counter = 0

    def version(self) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "--version"], 0, stdout="lark-cli version 1.0.56")

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return LarkCliResult(
            ["lark-cli", "auth", "status", "--json", "--verify"],
            0,
            json_data={"identities": {"bot": {"openId": "ou_bot", "available": True, "status": "ready"}}},
        )

    def owner_message(self, **kwargs: Any) -> LarkCliResult:
        self.calls.append(f"owner:{kwargs.get('dry_run')}")
        self.owner_calls.append(kwargs)
        return LarkCliResult(["lark-cli", "im", "+messages-send"], 0, json_data={"data": {"message_id": "om_owner_sent"}})

    def reply_message(self, **kwargs: Any) -> LarkCliResult:
        self.calls.append(f"reply:{kwargs.get('dry_run')}")
        self.reply_calls.append(kwargs)
        if kwargs.get("dry_run"):
            return LarkCliResult(["lark-cli", "im", "+messages-reply"], 0, json_data={"api": []})
        self.sent_counter += 1
        return LarkCliResult(
            ["lark-cli", "im", "+messages-reply"],
            0,
            json_data={"data": {"message_id": f"om_sent_{self.sent_counter}"}},
        )

    def get_messages(self, **kwargs: Any) -> MessagePage:
        message_id = kwargs["message_ids"][0]
        return MessagePage(
            [
                {
                    "message_id": message_id,
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "sender_id": "ou_bot",
                    "sender_type": "bot",
                    "create_time": "2026-06-22T10:00:00+08:00",
                    "content": {"text": "sent"},
                }
            ]
        )

    def list_p2p_messages(self, **kwargs: Any) -> MessagePage:
        self.calls.append("approval_inbox")
        if self.fail_approval_inbox:
            raise RuntimeError("approval inbox failed")
        return MessagePage([])

    def search_messages(self, **kwargs: Any) -> MessagePage:
        self.calls.append(f"search:{kwargs['chat_type']}:{kwargs['is_at_me']}")
        return MessagePage(self.search_items.get((kwargs["chat_type"], kwargs["is_at_me"]), []))

    def list_chat_messages(self, **kwargs: Any) -> MessagePage:
        self.calls.append(f"chat:{kwargs['chat_id']}")
        return MessagePage([])

    def list_thread_messages(self, **kwargs: Any) -> MessagePage:
        self.calls.append(f"thread:{kwargs['thread_id']}")
        return MessagePage([])

    def download_resource(self, **kwargs: Any) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "im", "+messages-resources-download"], 0, json_data={})


class FakeHermes:
    def __init__(self):
        self.session_calls = 0

    def task_router(self, prompt: str) -> HermesCliResult:
        return HermesCliResult(["hermes"], 0, json_data={"route": "ignore", "confidence": 1, "reason": ""})

    def task_session(self, prompt: str, *, session_id: str | None = None) -> HermesCliResult:
        self.session_calls += 1
        target = "om_group" if self.session_calls == 1 else "om_p2p"
        return HermesCliResult(
            ["hermes"],
            0,
            json_data={
                "task_label": "label",
                "task_state": "needs_reply",
                "answerability": "auto_reply",
                "confidence": 0.99,
                "proposed_reply": "reply text",
                "reply_target_message_id": target,
                "watch_action": "keep_watching",
                "watch_extend_minutes": 120,
                "risk_level": "low",
                "safety_notes": [],
                "requires_resources": False,
            },
            session_id=f"sid_{self.session_calls}",
        )


def test_startup_critical_failure_does_not_enter_loop(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite([HealthCheckResult("config_schema", "critical", "failed", "bad")])
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
    )

    assert daemon.run_forever() == 2

    assert "daemon_startup_health_failed" in (tmp_path / "agent.jsonl").read_text(encoding="utf-8")


def test_noop_tick_is_logged(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite([HealthCheckResult("config_schema", "critical", "ok", "ok")])
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
    )

    daemon.run_one_noop_tick(run_id="run_1")

    assert "daemon_tick_noop" in (tmp_path / "agent.jsonl").read_text(encoding="utf-8")


def test_keyboard_interrupt_finishes_run(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite([HealthCheckResult("config_schema", "critical", "ok", "ok")])

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
        run_metadata={"git_commit": "abc123", "git_dirty": True},
        sleep_func=interrupt,
    )

    assert daemon.run_forever() == 0
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, git_commit, git_dirty FROM runs WHERE run_id = ?",
            ("run_1",),
        ).fetchone()
    assert row["status"] == "interrupted"
    assert row["git_commit"] == "abc123"
    assert row["git_dirty"] == 1


def test_runtime_critical_health_failure_blocks_ingestion_and_all_sends(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    task_id = _insert_task(store)
    store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={"reply_target_message_id": "om_target", "text": "hello", "identity": "user"},
    )
    store.create_owner_notification_action(task_id=task_id, payload={"type": "notify"})
    suite = FakeHealthSuite([HealthCheckResult("lark_auth_verify", "critical", "failed", "bad")])
    fake = FakeFeishu()
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=False,
        app_config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        feishu_client=fake,  # type: ignore[arg-type]
        runtime_health_interval_seconds=0,
    )

    results = daemon.run_one_tick(run_id="run_1")

    assert [result.name for result in results] == ["runtime_health"]
    assert fake.reply_calls == []
    assert fake.owner_calls == []
    with store.connect() as conn:
        statuses = [row["status"] for row in conn.execute("SELECT status FROM actions ORDER BY id").fetchall()]
    assert statuses == ["pending", "pending"]


def test_approval_inbox_failure_blocks_send_reply_but_allows_owner_notification(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    task_id = _insert_task(store)
    send_action = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={"reply_target_message_id": "om_target", "text": "hello", "identity": "user"},
    )
    owner_action = store.create_owner_notification_action(task_id=task_id, payload={"type": "notify"})
    assert send_action is not None and owner_action is not None
    suite = FakeHealthSuite([HealthCheckResult("config_schema", "critical", "ok", "ok")])
    fake = FakeFeishu()
    fake.fail_approval_inbox = True
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    processor = TaskProcessingService(
        store=store,
        config=config,
        hermes_client=FakeHermes(),
        logger=logger,
    )
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=False,
        app_config=config,
        feishu_client=fake,  # type: ignore[arg-type]
        task_processor=processor,
        runtime_health_interval_seconds=0,
    )

    results = daemon.run_one_tick(run_id="run_1")

    assert [result.name for result in results] == [
        "approval_inbox",
        "group_at_me",
        "p2p",
        "active_watch",
        "dispatch",
    ]
    assert [call["dry_run"] for call in fake.reply_calls] == [True]
    assert [call["dry_run"] for call in fake.owner_calls] == [True, False]
    with store.connect() as conn:
        send = conn.execute("SELECT status, result_json FROM actions WHERE id = ?", (send_action,)).fetchone()
        owner = conn.execute("SELECT status, result_json FROM actions WHERE id = ?", (owner_action,)).fetchone()
    assert send["status"] == "pending"
    assert owner["status"] == "sent"
    assert "approval_inbox_failed" in send["result_json"]
    assert "approval_inbox_failed" not in (owner["result_json"] or "")


def test_fake_feishu_hermes_tick_runs_ordered_ingest_watch_and_dispatch(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite([HealthCheckResult("config_schema", "critical", "ok", "ok")])
    fake = FakeFeishu()
    fake.search_items[("group", True)] = [
        _raw_message("om_group", chat_id="oc_group", chat_type="group", mentions=[{"open_id": "ou_owner"}])
    ]
    fake.search_items[("p2p", False)] = [
        _raw_message("om_p2p", chat_id="ou_chat", chat_type="p2p")
    ]
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        chats={"oc_group": ChatPolicyConfig(auto_reply=True, bot_joined=True)},
    )
    processor = TaskProcessingService(
        store=store,
        config=config,
        hermes_client=FakeHermes(),
        logger=logger,
    )
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=False,
        app_config=config,
        feishu_client=fake,  # type: ignore[arg-type]
        task_processor=processor,
        runtime_health_interval_seconds=0,
    )

    results = daemon.run_one_tick(run_id="run_1")

    assert [result.name for result in results] == [
        "approval_inbox",
        "group_at_me",
        "p2p",
        "active_watch",
        "dispatch",
    ]
    assert fake.calls[:3] == [
        "approval_inbox",
        "search:group:True",
        "search:p2p:False",
    ]
    assert fake.calls.index("reply:True") > fake.calls.index("search:p2p:False")
    assert "chat:oc_group" in fake.calls
    with store.connect() as conn:
        sent_actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE status = 'sent'").fetchone()["c"]
    assert sent_actions == 2


def _insert_task(store: SQLiteStore) -> int:
    store.migrate()
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, created_at, updated_at, chat_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("t_1", "watching", "oc_1", "om_target", "label", "now", "now", "p2p"),
        )
    return int(cursor.lastrowid)


def _raw_message(
    message_id: str,
    *,
    chat_id: str,
    chat_type: str,
    mentions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {"text": "hello"}
    if mentions is not None:
        content["mentions"] = mentions
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": "ou_ext",
        "sender_type": "user",
        "sender_name": "Ext",
        "create_time": "2026-06-22T10:00:00+08:00",
        "content": content,
    }
