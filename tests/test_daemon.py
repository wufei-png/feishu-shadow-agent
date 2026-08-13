from __future__ import annotations

import json
import signal
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.cli import main
from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    LoadedConfig,
    OwnerConfig,
)
from feishu_shadow_agent.daemon import Daemon
from feishu_shadow_agent.health import REQUIRED_USER_SCOPES, HealthSuite
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.operator_query import OperatorQueryService
from feishu_shadow_agent.processing import TaskProcessingService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult, LarkCliResult, MessagePage


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


class SequenceHealthSuite:
    def __init__(self, results: list[list[HealthCheckResult]]):
        self.results = results
        self.run_id = "run_1"
        self.calls = 0

    def run(self, *, send_test: bool = False) -> list[HealthCheckResult]:
        return self.run_runtime_critical()

    def run_runtime_critical(self) -> list[HealthCheckResult]:
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[index]


class FakeFeishu:
    def __init__(self):
        self.fail_approval_inbox = False
        self.fail_reply_actual = False
        self.reply_calls: list[dict[str, Any]] = []
        self.owner_calls: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.search_items: dict[tuple[str, bool], list[dict[str, Any]]] = {}
        self.sent_counter = 0

    def version(self) -> LarkCliResult:
        return LarkCliResult(
            ["lark-cli", "--version"], 0, stdout="lark-cli version 1.0.56"
        )

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return LarkCliResult(
            ["lark-cli", "auth", "status", "--json", "--verify"],
            0,
            json_data={
                "identities": {
                    "user": {"scope": " ".join(sorted(REQUIRED_USER_SCOPES))},
                    "bot": {"openId": "ou_bot", "available": True, "status": "ready"},
                }
            },
        )

    def owner_message(self, **kwargs: Any) -> LarkCliResult:
        self.calls.append(f"owner:{kwargs.get('dry_run')}")
        self.owner_calls.append(kwargs)
        return LarkCliResult(
            ["lark-cli", "im", "+messages-send"],
            0,
            json_data={"data": {"message_id": "om_owner_sent"}},
        )

    def reply_message(self, **kwargs: Any) -> LarkCliResult:
        self.calls.append(f"reply:{kwargs.get('dry_run')}")
        self.reply_calls.append(kwargs)
        if kwargs.get("dry_run"):
            return LarkCliResult(
                ["lark-cli", "im", "+messages-reply"], 0, json_data={"api": []}
            )
        if self.fail_reply_actual:
            return LarkCliResult(
                ["lark-cli", "im", "+messages-reply"], 1, stderr="reply failed"
            )
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
        return MessagePage(
            self.search_items.get((kwargs["chat_type"], kwargs["is_at_me"]), [])
        )

    def list_chat_messages(self, **kwargs: Any) -> MessagePage:
        self.calls.append(f"chat:{kwargs['chat_id']}")
        return MessagePage([])

    def list_thread_messages(self, **kwargs: Any) -> MessagePage:
        self.calls.append(f"thread:{kwargs['thread_id']}")
        return MessagePage([])

    def download_resource(self, **kwargs: Any) -> LarkCliResult:
        return LarkCliResult(
            ["lark-cli", "im", "+messages-resources-download"], 0, json_data={}
        )


class FakeAgentBackend:
    provider = "hermes"

    def __init__(self):
        self.session_calls = 0

    def task_router(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        return AgentRunResult(
            ["hermes"],
            0,
            json_data={"route": "ignore", "target_task_id": None, "reason": ""},
        )

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        self.session_calls += 1
        target = "om_group" if self.session_calls == 1 else "om_p2p"
        return AgentRunResult(
            ["hermes"],
            0,
            json_data={
                "task_label": "label",
                "answerability": "auto_reply",
                "decision_reason": None,
                "proposed_reply": "reply text",
                "reply_target_message_id": target,
                "watch_action": "keep_watching",
            },
            session_id=f"sid_{self.session_calls}",
        )


def _seed_policy(store: SQLiteStore, config: AppConfig) -> None:
    store.import_product_policy_from_config(config)


def test_startup_critical_failure_does_not_enter_loop(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "failed", "bad")]
    )
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
    )

    assert daemon.run_forever() == 2

    assert "daemon_startup_health_failed" in (tmp_path / "agent.jsonl").read_text(
        encoding="utf-8"
    )


def test_startup_missing_product_policy_fails_closed_with_import_hint(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    loaded = LoadedConfig(
        config=config, path=tmp_path / "config.yaml", base_dir=tmp_path, raw={}
    )

    def ok_hermes(_loaded: LoadedConfig) -> HealthCheckResult:
        return HealthCheckResult("hermes_reachable", "critical", "ok", "ok")

    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=FakeFeishu(),  # type: ignore[arg-type]
        hermes_checker=ok_hermes,
        run_id="run_1",
    )
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,
        tick_interval_seconds=1,
        dry_run=False,
        app_config=config,
        feishu_client=FakeFeishu(),  # type: ignore[arg-type]
    )

    assert daemon.run_forever() == 2

    log = (tmp_path / "agent.jsonl").read_text(encoding="utf-8")
    assert "daemon_startup_health_failed" in log
    assert "product_policy_initialized" in log
    assert "policy import-config" in log
    assert "daemon_started" not in log


def test_noop_tick_is_logged(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
    )

    daemon.run_one_noop_tick(run_id="run_1")

    assert "daemon_tick_noop" in (tmp_path / "agent.jsonl").read_text(encoding="utf-8")


def test_tick_heartbeat_records_start_finish_and_summary(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
    )

    daemon.run_one_tick(run_id="run_1")

    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT last_heartbeat_at, last_tick_started_at, last_tick_finished_at,
                   last_tick_status, last_tick_summary_json
            FROM runs WHERE run_id = ?
            """,
            ("run_1",),
        ).fetchone()
    summary = json.loads(row["last_tick_summary_json"])
    assert row["last_heartbeat_at"] is not None
    assert row["last_tick_started_at"] is not None
    assert row["last_tick_finished_at"] is not None
    assert row["last_tick_status"] == "ok"
    assert summary["stages"] == [
        {"name": "noop", "ok": True, "processed": 0, "error": None}
    ]


def test_daemon_tick_expires_overdue_approvals_before_approval_inbox(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class TrackingStore(SQLiteStore):
        def expire_pending_approvals(self, *, now: str | None = None) -> int:
            events.append("expire")
            return super().expire_pending_approvals(now=now)

    class TrackingFeishu(FakeFeishu):
        def list_p2p_messages(self, **kwargs: Any) -> MessagePage:
            events.append("approval_inbox")
            return super().list_p2p_messages(**kwargs)

    store = TrackingStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    task_id = _insert_task(store)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO approvals(
              short_id, task_id, kind, status, payload_json, preview, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a_overdue",
                task_id,
                "send_reply",
                "pending",
                json.dumps({"reply_target_message_id": "om_target", "text": "reply"}),
                "reply",
                "2026-06-22T08:00:00+08:00",
                "2000-01-01T00:00:00+00:00",
            ),
        )
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    _seed_policy(store, config)
    processor = TaskProcessingService(
        store=store,
        config=config,
        agent_backend=FakeAgentBackend(),
        logger=logger,
        agent_retry_delays_seconds=(0.0, 0.0),
    )
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
        app_config=config,
        feishu_client=TrackingFeishu(),  # type: ignore[arg-type]
        task_processor=processor,
        runtime_health_interval_seconds=0,
    )

    daemon.run_one_tick(run_id="run_1")

    assert events[0] == "expire"
    assert events.index("expire") < events.index("approval_inbox")
    with store.connect() as conn:
        approval = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE short_id = ?",
            ("a_overdue",),
        ).fetchone()
    assert approval["status"] == "expired"
    assert approval["resolved_at"] is not None


def test_keyboard_interrupt_finishes_run(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )

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
    assert row["status"] == "stopped"
    assert row["git_commit"] == "abc123"
    assert row["git_dirty"] == 1


def test_unhandled_exception_marks_daemon_crashed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )

    def crash(_seconds: float) -> None:
        raise RuntimeError("loop exploded")

    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
        sleep_func=crash,
    )

    with pytest.raises(RuntimeError, match="loop exploded"):
        daemon.run_forever()

    with store.connect() as conn:
        run = conn.execute(
            "SELECT status, finished_at FROM runs WHERE run_id = ?", ("run_1",)
        ).fetchone()
    assert run["status"] == "crashed"
    assert run["finished_at"] is not None
    assert "daemon_crashed" in (tmp_path / "agent.jsonl").read_text(encoding="utf-8")


def test_wake_runs_next_tick_without_waiting_for_poll_interval(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    first_tick = threading.Event()
    results: list[int] = []

    class WakeableDaemon(Daemon):
        tick_count = 0

        def run_one_tick(self, *, run_id: str) -> list[Any]:
            self.tick_count += 1
            if self.tick_count == 1:
                first_tick.set()
            else:
                self.request_stop()
            return []

    daemon = WakeableDaemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=30,
        dry_run=True,
    )
    thread = threading.Thread(target=lambda: results.append(daemon.run_forever()))
    thread.start()
    assert first_tick.wait(timeout=1)

    daemon.wake()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert daemon.tick_count == 2
    assert results == [0]
    with store.connect() as conn:
        status = conn.execute(
            "SELECT status FROM runs WHERE run_id = ?", ("run_1",)
        ).fetchone()["status"]
    assert status == "stopped"


def test_sigint_and_sigterm_handlers_request_controlled_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: dict[int, Any] = {}
    previous_handler = object()
    monkeypatch.setattr(signal, "getsignal", lambda _signum: previous_handler)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: registered.__setitem__(signum, handler),
    )
    daemon = Daemon(
        store=SQLiteStore(tmp_path / "agent.sqlite3"),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        health_suite=FakeHealthSuite([]),  # type: ignore[arg-type]
        tick_interval_seconds=30,
        dry_run=True,
    )

    previous = daemon._install_signal_handlers()
    registered[signal.SIGTERM](signal.SIGTERM, None)

    assert set(previous) == {signal.SIGINT, signal.SIGTERM}
    assert daemon._stop_is_requested() is True


def test_runtime_critical_health_failure_blocks_ingestion_and_all_sends(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    task_id = _insert_task(store)
    store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    store.create_owner_notification_action(task_id=task_id, payload={"type": "notify"})
    suite = FakeHealthSuite(
        [HealthCheckResult("lark_auth_verify", "critical", "failed", "bad")]
    )
    fake = FakeFeishu()
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    _seed_policy(store, config)
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=False,
        app_config=config,
        feishu_client=fake,  # type: ignore[arg-type]
        runtime_health_interval_seconds=0,
    )

    results = daemon.run_one_tick(run_id="run_1")

    assert [result.name for result in results] == ["runtime_health"]
    assert fake.reply_calls == []
    assert fake.owner_calls == []
    with store.connect() as conn:
        statuses = [
            row["status"]
            for row in conn.execute("SELECT status FROM actions ORDER BY id").fetchall()
        ]
    assert statuses == ["pending", "pending"]
    with store.connect() as conn:
        run = conn.execute(
            "SELECT last_tick_status, last_tick_summary_json FROM runs WHERE run_id = ?",
            ("run_1",),
        ).fetchone()
    assert run["last_tick_status"] == "failed"
    assert (
        json.loads(run["last_tick_summary_json"])["stages"][0]["name"]
        == "runtime_health"
    )


def test_dashboard_snapshot_flags_stale_running_daemon(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.initialize()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO runs(
              run_id, started_at, status, dry_run, last_heartbeat_at,
              last_tick_started_at, last_tick_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run_stale",
                "2026-06-22T10:00:00+08:00",
                "running",
                1,
                "2026-06-22T10:00:00+08:00",
                "2026-06-22T10:00:00+08:00",
                "running",
            ),
        )

    snapshot = OperatorQueryService(
        store,
        now=lambda: "2026-06-22T10:02:01+08:00",
    ).dashboard_snapshot(
        daemon_stale_after_seconds=60,
    )

    assert snapshot["daemon_liveness"]["status"] == "stale"
    assert snapshot["daemon_liveness"]["heartbeat_age_seconds"] == 121


def test_dashboard_snapshot_daemon_liveness_ignores_newer_doctor_run(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.initialize()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO runs(
              run_id, started_at, status, dry_run, last_heartbeat_at,
              last_tick_started_at, last_tick_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run_stale_daemon",
                "2026-06-22T10:00:00+08:00",
                "running",
                1,
                "2026-06-22T10:00:00+08:00",
                "2026-06-22T10:00:00+08:00",
                "running",
            ),
        )
        conn.execute(
            """
            INSERT INTO runs(
              run_id, started_at, finished_at, status, dry_run, last_heartbeat_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "doctor_newer",
                "2026-06-22T10:01:00+08:00",
                "2026-06-22T10:01:05+08:00",
                "ok",
                1,
                "2026-06-22T10:01:00+08:00",
            ),
        )

    snapshot = OperatorQueryService(
        store,
        now=lambda: "2026-06-22T10:02:01+08:00",
    ).dashboard_snapshot(
        daemon_stale_after_seconds=60,
    )

    assert snapshot["last_run"]["run_id"] == "doctor_newer"
    assert snapshot["daemon_liveness"]["run_id"] == "run_stale_daemon"
    assert snapshot["daemon_liveness"]["status"] == "stale"
    assert snapshot["daemon_liveness"]["heartbeat_age_seconds"] == 121


def test_runtime_health_failure_rechecks_on_retry_interval_and_recovers(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = 0.0
    monkeypatch.setattr("feishu_shadow_agent.daemon.time.monotonic", lambda: now)
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
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
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    suite = SequenceHealthSuite(
        [
            [HealthCheckResult("lark_auth_verify", "critical", "failed", "bad")],
            [HealthCheckResult("lark_auth_verify", "critical", "ok", "ok")],
        ]
    )
    fake = FakeFeishu()
    _seed_policy(store, config)
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=False,
        app_config=config,
        feishu_client=fake,  # type: ignore[arg-type]
    )

    first = daemon.run_one_tick(run_id="run_1")
    now = config.health.retry_interval_seconds - 1
    second = daemon.run_one_tick(run_id="run_1")
    now = config.health.retry_interval_seconds
    third = daemon.run_one_tick(run_id="run_1")

    assert [result.name for result in first] == ["runtime_health"]
    assert [result.name for result in second] == ["runtime_health"]
    assert [result.name for result in third] == [
        "approval_inbox",
        "group_at_me",
        "p2p",
        "active_watch",
        "dispatch",
        "retention",
    ]
    assert suite.calls == 2
    assert [call["dry_run"] for call in fake.reply_calls] == [True, False]
    with store.connect() as conn:
        action = conn.execute(
            "SELECT status FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
    assert action["status"] == "sent"


def test_runtime_health_ok_uses_interval_seconds_before_next_refresh(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = 0.0
    monkeypatch.setattr("feishu_shadow_agent.daemon.time.monotonic", lambda: now)
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    suite = SequenceHealthSuite(
        [
            [HealthCheckResult("lark_auth_verify", "critical", "ok", "ok")],
            [HealthCheckResult("lark_auth_verify", "critical", "ok", "ok")],
        ]
    )
    _seed_policy(store, config)
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
        app_config=config,
        feishu_client=FakeFeishu(),  # type: ignore[arg-type]
    )

    daemon.run_one_tick(run_id="run_1")
    now = config.health.retry_interval_seconds
    daemon.run_one_tick(run_id="run_1")
    now = config.health.interval_seconds
    daemon.run_one_tick(run_id="run_1")

    assert suite.calls == 2


def test_product_policy_missing_blocks_tick_even_when_runtime_health_is_cached(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = 0.0
    monkeypatch.setattr("feishu_shadow_agent.daemon.time.monotonic", lambda: now)
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    _seed_policy(store, config)
    suite = SequenceHealthSuite(
        [
            [HealthCheckResult("lark_auth_verify", "critical", "ok", "ok")],
            [HealthCheckResult("lark_auth_verify", "critical", "ok", "ok")],
        ]
    )
    fake = FakeFeishu()
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=False,
        app_config=config,
        feishu_client=fake,  # type: ignore[arg-type]
    )
    first = daemon.run_one_tick(run_id="run_1")
    fake.calls.clear()
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
    with store.connect() as conn:
        conn.execute("DELETE FROM product_policies WHERE key = ?", ("reply_policy",))
    now = 1.0

    second = daemon.run_one_tick(run_id="run_1")

    assert [result.name for result in first] == [
        "approval_inbox",
        "group_at_me",
        "p2p",
        "active_watch",
        "dispatch",
        "retention",
    ]
    assert [result.name for result in second] == ["runtime_health"]
    assert suite.calls == 1
    assert fake.calls == []
    assert fake.reply_calls == []
    with store.connect() as conn:
        action = conn.execute(
            "SELECT status FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        health = conn.execute(
            """
            SELECT check_name, status, message
            FROM health_checks
            WHERE check_name = 'product_policy_initialized'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert action["status"] == "pending"
    assert health["status"] == "failed"
    assert "policy import-config" in health["message"]


def test_approval_inbox_failure_blocks_send_reply_but_allows_owner_notification(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    task_id = _insert_task(store)
    send_action = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_target",
        payload={
            "reply_target_message_id": "om_target",
            "text": "hello",
            "identity": "user",
        },
    )
    owner_action = store.create_owner_notification_action(
        task_id=task_id, payload={"type": "notify"}
    )
    assert send_action is not None and owner_action is not None
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    fake = FakeFeishu()
    fake.fail_approval_inbox = True
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    _seed_policy(store, config)
    processor = TaskProcessingService(
        store=store,
        config=config,
        agent_backend=FakeAgentBackend(),
        logger=logger,
        agent_retry_delays_seconds=(0.0, 0.0),
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
        "retention",
    ]
    assert [call["dry_run"] for call in fake.reply_calls] == [True]
    assert [call["dry_run"] for call in fake.owner_calls] == [True, False]
    with store.connect() as conn:
        send = conn.execute(
            "SELECT status, result_json FROM actions WHERE id = ?", (send_action,)
        ).fetchone()
        owner = conn.execute(
            "SELECT status, result_json FROM actions WHERE id = ?", (owner_action,)
        ).fetchone()
        run = conn.execute(
            "SELECT last_tick_status, last_tick_summary_json FROM runs WHERE run_id = ?",
            ("run_1",),
        ).fetchone()
    assert send["status"] == "pending"
    assert owner["status"] == "sent"
    assert "approval_inbox_failed" in send["result_json"]
    assert "approval_inbox_failed" not in (owner["result_json"] or "")
    assert run["last_tick_status"] == "partial_failed"
    assert json.loads(run["last_tick_summary_json"])["failed"] == 1


def test_approval_inbox_failure_without_pending_send_reply_is_visible_in_status(
    tmp_path: Path,
    capsys: Any,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
storage:
  sqlite_path: agent.sqlite3
logging:
  jsonl_path: agent.jsonl
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    fake = FakeFeishu()
    fake.fail_approval_inbox = True
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    _seed_policy(store, config)
    processor = TaskProcessingService(
        store=store,
        config=config,
        agent_backend=FakeAgentBackend(),
        logger=logger,
        agent_retry_delays_seconds=(0.0, 0.0),
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

    daemon.run_one_tick(run_id="run_1")

    snapshot = OperatorQueryService(store).dashboard_snapshot()
    warnings = snapshot["recent_health_warnings"]
    assert warnings[0]["check_name"] == "approval_inbox"
    assert warnings[0]["severity"] == "warning"
    assert warnings[0]["status"] == "failed"
    assert "approval inbox failed" in warnings[0]["message"]
    with store.connect() as conn:
        send_reply_count = conn.execute(
            "SELECT COUNT(*) AS count FROM actions WHERE kind = 'send_reply'"
        ).fetchone()["count"]
    assert send_reply_count == 0

    assert main(["status", "--config", str(config_path)]) == 0
    output = yaml.safe_load(capsys.readouterr().out)
    assert output["recent_health_warnings"][0]["check_name"] == "approval_inbox"


def test_approval_inbox_success_clears_prior_health_issue(
    tmp_path: Path,
    capsys: Any,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
storage:
  sqlite_path: agent.sqlite3
logging:
  jsonl_path: agent.jsonl
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    fake = FakeFeishu()
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    _seed_policy(store, config)
    processor = TaskProcessingService(
        store=store,
        config=config,
        agent_backend=FakeAgentBackend(),
        logger=logger,
        agent_retry_delays_seconds=(0.0, 0.0),
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

    fake.fail_approval_inbox = True
    daemon.run_one_tick(run_id="run_failed")
    fake.fail_approval_inbox = False
    daemon.run_one_tick(run_id="run_recovered")

    with store.connect() as conn:
        latest = conn.execute(
            """
            SELECT status, message, details_json
            FROM health_checks
            WHERE check_name = 'approval_inbox'
            ORDER BY datetime(checked_at) DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    assert latest["status"] == "ok"
    assert latest["message"] == "approval_inbox completed successfully"
    assert json.loads(latest["details_json"])["processed"] == 0

    payload = OperatorQueryService(store).health_issues()
    assert not any(
        issue["title"] == "approval_inbox reported failed"
        for issue in payload["issues"]
    )
    dashboard = OperatorQueryService(store).dashboard_snapshot()
    assert not any(
        warning["check_name"] == "approval_inbox"
        for warning in dashboard["recent_health_warnings"]
    )
    assert main(["status", "--config", str(config_path)]) == 0
    output = yaml.safe_load(capsys.readouterr().out)
    assert not any(
        warning["check_name"] == "approval_inbox"
        for warning in output["recent_health_warnings"]
    )


def test_approval_inbox_success_does_not_write_repeated_health_ok(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    fake = FakeFeishu()
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    _seed_policy(store, config)
    processor = TaskProcessingService(
        store=store,
        config=config,
        agent_backend=FakeAgentBackend(),
        logger=logger,
        agent_retry_delays_seconds=(0.0, 0.0),
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

    daemon.run_one_tick(run_id="run_first_ok")
    daemon.run_one_tick(run_id="run_second_ok")

    with store.connect() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM health_checks
            WHERE check_name = 'approval_inbox'
            """
        ).fetchone()["count"]
    assert count == 0


def test_fake_feishu_hermes_tick_runs_ordered_ingest_watch_and_dispatch(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    fake = FakeFeishu()
    fake.search_items[("group", True)] = [
        _raw_message(
            "om_group",
            chat_id="oc_group",
            chat_type="group",
            mentions=[{"open_id": "ou_owner"}],
        )
    ]
    fake.search_items[("p2p", False)] = [
        _raw_message("om_p2p", chat_id="ou_chat", chat_type="p2p")
    ]
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        chats={"oc_group": ChatPolicyConfig(auto_reply=True, bot_joined=True)},
    )
    _seed_policy(store, config)
    processor = TaskProcessingService(
        store=store,
        config=config,
        agent_backend=FakeAgentBackend(),
        logger=logger,
        agent_retry_delays_seconds=(0.0, 0.0),
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
        "retention",
    ]
    assert fake.calls[:3] == [
        "approval_inbox",
        "search:group:True",
        "search:p2p:False",
    ]
    assert fake.calls.index("reply:True") > fake.calls.index("search:p2p:False")
    assert "chat:oc_group" in fake.calls
    with store.connect() as conn:
        sent_actions = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE status = 'sent'"
        ).fetchone()["c"]
    assert sent_actions == 2


def test_dispatch_failure_is_reflected_in_tick_heartbeat_summary(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    suite = FakeHealthSuite(
        [HealthCheckResult("config_schema", "critical", "ok", "ok")]
    )
    fake = FakeFeishu()
    fake.fail_reply_actual = True
    fake.search_items[("group", True)] = [
        _raw_message(
            "om_group",
            chat_id="oc_group",
            chat_type="group",
            mentions=[{"open_id": "ou_owner"}],
        )
    ]
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        chats={"oc_group": ChatPolicyConfig(auto_reply=True, bot_joined=True)},
    )
    _seed_policy(store, config)
    processor = TaskProcessingService(
        store=store,
        config=config,
        agent_backend=FakeAgentBackend(),
        logger=logger,
        agent_retry_delays_seconds=(0.0, 0.0),
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

    dispatch_result = next(result for result in results if result.name == "dispatch")
    assert dispatch_result.ok is False
    assert dispatch_result.error == "1 dispatch action(s) failed"
    with store.connect() as conn:
        run = conn.execute(
            "SELECT last_tick_status, last_tick_summary_json FROM runs WHERE run_id = ?",
            ("run_1",),
        ).fetchone()
    summary = json.loads(run["last_tick_summary_json"])
    dispatch_stage = next(
        stage for stage in summary["stages"] if stage["name"] == "dispatch"
    )
    assert run["last_tick_status"] == "partial_failed"
    assert dispatch_stage["ok"] is False
    assert dispatch_stage["error"] == "1 dispatch action(s) failed"


def _insert_task(store: SQLiteStore) -> int:
    store.initialize()
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
