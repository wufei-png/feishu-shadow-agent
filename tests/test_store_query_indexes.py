from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from feishu_shadow_agent.store.sqlite_store import (
    LATEST_NON_OK_HEALTH_CHECKS_SQL,
    SQLiteStore,
)

# Hot runtime queries kept here in the shape the store actually issues, so the
# plan assertions protect the access path rather than a rewritten variant.
LATEST_HEALTH_STATUS_SQL = """
SELECT status
FROM health_checks
WHERE check_name = ?
ORDER BY julianday(checked_at) DESC, id DESC
LIMIT 1
"""
STALE_SENDING_SQL = """
SELECT *
FROM actions
WHERE status = 'sending'
  AND julianday(updated_at) <= julianday(?)
ORDER BY updated_at, id
"""
EXPIRE_APPROVALS_SQL = """
SELECT *
FROM approvals
WHERE status = 'pending'
  AND expires_at IS NOT NULL
  AND julianday(expires_at) < julianday(?)
ORDER BY expires_at, id
"""
NOW = "2026-01-01T00:00:00+00:00"
SEEDED_RENDER_BUDGET_SECONDS = 5.0


def _plan(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[str]:
    return [str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]


def _initialized_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.initialize()
    return store


def _seed_health_rows(
    store: SQLiteStore,
    *,
    names: tuple[str, ...],
    rows_per_name: int,
    status: str,
) -> None:
    with store.connect() as conn:
        conn.executemany(
            """
            INSERT INTO health_checks(check_name, severity, status, message, checked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    name,
                    "warning",
                    status,
                    "seeded",
                    f"2026-01-01T00:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
                )
                for name in names
                for index in range(rows_per_name)
            ],
        )


def test_latest_health_issue_query_reports_one_row_per_check(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    with store.connect() as conn:
        conn.executemany(
            """
            INSERT INTO health_checks(check_name, severity, status, message, checked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    "check_recovered",
                    "warning",
                    "failed",
                    "old",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "check_recovered",
                    "warning",
                    "ok",
                    "new",
                    "2026-01-01T00:01:00+00:00",
                ),
                ("check_open", "critical", "ok", "old", "2026-01-01T00:00:00+00:00"),
                (
                    "check_open",
                    "critical",
                    "failed",
                    "new",
                    "2026-01-01T00:02:00+00:00",
                ),
            ],
        )
        rows = conn.execute(LATEST_NON_OK_HEALTH_CHECKS_SQL, (20,)).fetchall()

    assert [row["check_name"] for row in rows] == ["check_open"]
    assert rows[0]["message"] == "new"


def test_latest_health_issue_query_uses_covering_index(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    _seed_health_rows(
        store, names=("check_a", "check_b"), rows_per_name=50, status="failed"
    )

    with store.connect() as conn:
        plan = _plan(conn, LATEST_NON_OK_HEALTH_CHECKS_SQL, (20,))

    assert any("idx_health_checks_name_checked" in step for step in plan)
    assert not any(
        step.startswith("SCAN TABLE health_checks AS newer") for step in plan
    )


def test_latest_health_issue_query_stays_linear_with_many_warnings(
    tmp_path: Path,
) -> None:
    store = _initialized_store(tmp_path)
    names = ("check_a", "check_b", "check_c", "check_d")
    _seed_health_rows(store, names=names, rows_per_name=5_000, status="warning")

    with store.connect() as conn:
        plan = _plan(conn, LATEST_NON_OK_HEALTH_CHECKS_SQL, (20,))
        started = time.perf_counter()
        rows = conn.execute(LATEST_NON_OK_HEALTH_CHECKS_SQL, (20,)).fetchall()
        elapsed = time.perf_counter() - started

    assert any("idx_health_checks_name_checked" in step for step in plan)
    assert {row["check_name"] for row in rows} == set(names)
    assert elapsed < SEEDED_RENDER_BUDGET_SECONDS


def test_latest_health_status_lookup_uses_index(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    _seed_health_rows(store, names=("check_a",), rows_per_name=2_000, status="ok")

    with store.connect() as conn:
        plan = _plan(conn, LATEST_HEALTH_STATUS_SQL, ("check_a",))

    assert any("idx_health_checks_name_checked" in step for step in plan)


def test_dispatch_and_approval_hot_queries_use_indexes(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    with store.connect() as conn:
        conn.executemany(
            """
            INSERT INTO actions(idempotency_key, kind, status, created_at, updated_at)
            VALUES (?, 'send_reply', ?, ?, ?)
            """,
            [
                (f"key_{index}", "pending" if index % 2 else "sent", NOW, NOW)
                for index in range(500)
            ],
        )
        conn.executemany(
            """
            INSERT INTO approvals(short_id, kind, status, created_at, expires_at)
            VALUES (?, 'send_reply', 'pending', ?, ?)
            """,
            [(f"ap_{index}", NOW, NOW) for index in range(100)],
        )

        stale_plan = _plan(conn, STALE_SENDING_SQL, (NOW,))
        expire_plan = _plan(conn, EXPIRE_APPROVALS_SQL, (NOW,))

    assert any("idx_actions_status_updated" in step for step in stale_plan)
    assert any("idx_approvals_status_expires" in step for step in expire_plan)
