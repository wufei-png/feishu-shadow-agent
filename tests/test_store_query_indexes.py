from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from feishu_shadow_agent.store.sqlite_store import (
    EXPIRE_PENDING_APPROVALS_SQL,
    LATEST_HEALTH_CHECK_STATUS_SQL,
    LATEST_NON_OK_HEALTH_CHECKS_SQL,
    STALE_SENDING_ACTIONS_SQL,
    SQLiteStore,
)

# Queries are imported from the store rather than copied so these guards keep
# following the statements the runtime actually issues.
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
    # The correlated probe must read the index; a bare SCAN here is the
    # quadratic path this index exists to remove.
    assert any("newer" in step and "INDEX" in step for step in plan)
    assert not any(step.startswith("SCAN") and "newer" in step for step in plan)


def test_health_index_keeps_descending_key_for_early_exit(tmp_path: Path) -> None:
    """Descending order lets the probe stop at the newest row for a check.

    An ascending index yields the same query plan but degrades to a full scan
    per check, so the DDL itself has to stay ordered.
    """
    store = _initialized_store(tmp_path)
    with store.connect() as conn:
        ddl = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_health_checks_name_checked'
            """
        ).fetchone()[0]

    assert "checked_at DESC" in ddl
    assert "id DESC" in ddl


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
        plan = _plan(conn, LATEST_HEALTH_CHECK_STATUS_SQL, ("check_a",))

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

        stale_plan = _plan(conn, STALE_SENDING_ACTIONS_SQL, (NOW,))
        expire_plan = _plan(conn, EXPIRE_PENDING_APPROVALS_SQL, (NOW,))

    assert any("idx_actions_status_updated" in step for step in stale_plan)
    assert any("idx_approvals_status_expires" in step for step in expire_plan)
