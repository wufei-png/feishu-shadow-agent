from __future__ import annotations

import json
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from ..types import HealthCheckResult, utc_now_iso


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            sql = resources.files("feishu_shadow_agent.store").joinpath(
                "migrations/0001_foundation.sql"
            ).read_text(encoding="utf-8")
            conn.executescript(sql)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                ("0001_foundation", utc_now_iso()),
            )

    def health_probe(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT 1").fetchone()

    def record_run_start(
        self,
        *,
        run_id: str,
        dry_run: bool,
        git_commit: str | None = None,
        git_dirty: bool | None = None,
    ) -> None:
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs(
                  run_id, started_at, finished_at, status, dry_run, git_commit, git_dirty
                ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    utc_now_iso(),
                    "running",
                    int(dry_run),
                    git_commit,
                    None if git_dirty is None else int(git_dirty),
                ),
            )

    def record_run_finish(
        self,
        *,
        run_id: str,
        status: str,
        health_summary: dict[str, Any] | None = None,
    ) -> None:
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, health_summary_json = ?
                WHERE run_id = ?
                """,
                (
                    utc_now_iso(),
                    status,
                    json.dumps(health_summary or {}, ensure_ascii=False, default=str),
                    run_id,
                ),
            )

    def record_health_results(
        self,
        *,
        run_id: str | None,
        results: Iterable[HealthCheckResult],
    ) -> None:
        self.migrate()
        with self.connect() as conn:
            if run_id is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO runs(run_id, started_at, status, dry_run)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, utc_now_iso(), "running", 1),
                )
            conn.executemany(
                """
                INSERT INTO health_checks(
                  run_id, check_name, severity, status, message, details_json, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        result.name,
                        result.severity,
                        result.status,
                        result.message,
                        json.dumps(result.details, ensure_ascii=False, default=str),
                        utc_now_iso(),
                    )
                    for result in results
                ],
            )

    def set_checkpoint(self, key: str, value: dict[str, Any]) -> None:
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value_json = excluded.value_json,
                  updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False, default=str), utc_now_iso()),
            )

    def get_checkpoint(self, key: str) -> dict[str, Any] | None:
        self.migrate()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM checkpoints WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])
