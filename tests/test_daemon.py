from __future__ import annotations

from pathlib import Path

from feishu_shadow_agent.daemon import Daemon
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult


class FakeHealthSuite:
    def __init__(self, results: list[HealthCheckResult]):
        self.results = results
        self.run_id = "run_1"
        self.calls = 0

    def run(self, *, send_test: bool = False) -> list[HealthCheckResult]:
        self.calls += 1
        return self.results


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
