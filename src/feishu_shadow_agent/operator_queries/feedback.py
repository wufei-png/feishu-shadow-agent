from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Literal

from .common import OperatorQueryReadError, OperatorQueryUnavailable

FeedbackExecutionMode = Literal["production", "dry_run", "all"]
_DIFF_TOKEN_RE = re.compile(r"\s+|[^\s]+")


class FeedbackQuery:
    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        now: Callable[[], str],
    ):
        self._connect = connect
        self._now = now

    def overview(
        self,
        *,
        windows: tuple[int, ...] = (7, 30),
        recent_days: int = 30,
        recent_limit: int = 50,
        execution_mode: FeedbackExecutionMode = "production",
    ) -> dict[str, Any]:
        normalized_windows = tuple(_days(value) for value in windows)
        return {
            "generated_at": self._now(),
            "execution_mode": _execution_mode(execution_mode),
            "windows": [
                self.metrics(days=days, execution_mode=execution_mode)
                for days in normalized_windows
            ],
            "recent": self.list_feedback(
                days=recent_days,
                limit=recent_limit,
                execution_mode=execution_mode,
            ),
        }

    def metrics(
        self,
        *,
        days: int,
        execution_mode: FeedbackExecutionMode = "production",
    ) -> dict[str, Any]:
        normalized_days = _days(days)
        mode = _execution_mode(execution_mode)
        since = _since(self._now(), normalized_days)
        mode_sql, mode_params = _mode_filter(mode)
        try:
            with self._connect() as conn:
                summary = conn.execute(
                    f"""
                    SELECT
                      COUNT(*) AS total,
                      SUM(CASE WHEN outcome = 'suggestion_sent' THEN 1 ELSE 0 END) AS suggestion_sent,
                      SUM(CASE WHEN outcome = 'edited_sent' THEN 1 ELSE 0 END) AS edited_sent,
                      SUM(CASE WHEN outcome IN ('no_send_keep_watching', 'no_send_end_task') THEN 1 ELSE 0 END) AS no_send,
                      SUM(CASE WHEN feedback_reason IS NOT NULL THEN 1 ELSE 0 END) AS feedback_reason_count,
                      SUM(CASE WHEN content_expired_at IS NOT NULL THEN 1 ELSE 0 END) AS content_expired_count,
                      SUM(CASE
                            WHEN outcome = 'edited_sent'
                             AND content_expired_at IS NULL
                             AND suggested_reply IS NOT NULL
                             AND final_reply IS NOT NULL
                             AND suggested_reply != final_reply
                            THEN 1 ELSE 0
                          END) AS changed_reply_count
                    FROM approval_feedback
                    WHERE datetime(created_at) >= datetime(?)
                    {mode_sql}
                    """,
                    [since, *mode_params],
                ).fetchone()
                outcome_rows = _group_counts(
                    conn,
                    column="outcome",
                    since=since,
                    mode_sql=mode_sql,
                    mode_params=mode_params,
                )
                decision_rows = _group_counts(
                    conn,
                    column="decision_reason",
                    since=since,
                    mode_sql=mode_sql,
                    mode_params=mode_params,
                )
                feedback_rows = _group_counts(
                    conn,
                    column="feedback_reason",
                    since=since,
                    mode_sql=mode_sql,
                    mode_params=mode_params,
                )
        except OperatorQueryUnavailable:
            summary = None
            outcome_rows = []
            decision_rows = []
            feedback_rows = []
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise OperatorQueryReadError(str(exc)) from exc
            summary = None
            outcome_rows = []
            decision_rows = []
            feedback_rows = []

        values = dict(summary) if summary is not None else {}
        total = int(values.get("total") or 0)
        suggestion_sent = int(values.get("suggestion_sent") or 0)
        edited_sent = int(values.get("edited_sent") or 0)
        no_send = int(values.get("no_send") or 0)
        sent = suggestion_sent + edited_sent
        return {
            "days": normalized_days,
            "since": since,
            "total": total,
            "suggestion_sent": suggestion_sent,
            "edited_sent": edited_sent,
            "no_send": no_send,
            "changed_reply_count": int(values.get("changed_reply_count") or 0),
            "feedback_reason_count": int(values.get("feedback_reason_count") or 0),
            "content_expired_count": int(values.get("content_expired_count") or 0),
            "sent_without_edit_rate": _ratio(suggestion_sent, sent),
            "edit_rate_among_sends": _ratio(edited_sent, sent),
            "no_send_rate": _ratio(no_send, total),
            "by_outcome": _count_dtos(outcome_rows, total=total),
            "by_decision_reason": _count_dtos(decision_rows, total=total),
            "by_feedback_reason": _count_dtos(feedback_rows, total=total),
        }

    def list_feedback(
        self,
        *,
        days: int = 30,
        limit: int = 50,
        offset: int = 0,
        execution_mode: FeedbackExecutionMode = "production",
    ) -> list[dict[str, Any]]:
        normalized_days = _days(days)
        mode = _execution_mode(execution_mode)
        since = _since(self._now(), normalized_days)
        mode_sql, mode_params = _mode_filter(mode)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT f.*, a.short_id AS approval_short_id,
                           t.short_id AS task_short_id
                    FROM approval_feedback f
                    JOIN approvals a ON a.id = f.approval_id
                    LEFT JOIN tasks t ON t.id = f.task_id
                    WHERE datetime(f.created_at) >= datetime(?)
                    {mode_sql.replace("execution_mode", "f.execution_mode")}
                    ORDER BY datetime(f.created_at) DESC, f.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [since, *mode_params, _limit(limit), _offset(offset)],
                ).fetchall()
        except OperatorQueryUnavailable:
            return []
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise OperatorQueryReadError(str(exc)) from exc
        return [_feedback_dto(row) for row in rows]


def _group_counts(
    conn: sqlite3.Connection,
    *,
    column: str,
    since: str,
    mode_sql: str,
    mode_params: list[str],
) -> list[sqlite3.Row]:
    if column not in {"outcome", "decision_reason", "feedback_reason"}:
        raise ValueError("unsupported feedback grouping")
    return conn.execute(
        f"""
        SELECT COALESCE({column}, 'unclassified') AS value, COUNT(*) AS count
        FROM approval_feedback
        WHERE datetime(created_at) >= datetime(?)
        {mode_sql}
        GROUP BY COALESCE({column}, 'unclassified')
        ORDER BY count DESC, value
        """,
        [since, *mode_params],
    ).fetchall()


def _feedback_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "approval_id": data["approval_short_id"],
        "task_id": data["task_short_id"],
        "outcome": data["outcome"],
        "decision_reason": data["decision_reason"],
        "feedback_reason": data["feedback_reason"],
        "note": data["note"],
        "actor": data["actor"],
        "execution_mode": data["execution_mode"],
        "content_expired_at": data["content_expired_at"],
        "created_at": data["created_at"],
        "reply_comparison": _reply_comparison(
            outcome=data["outcome"],
            suggested=data["suggested_reply"],
            final=data["final_reply"],
            expired_at=data["content_expired_at"],
        ),
    }


def _reply_comparison(
    *, outcome: Any, suggested: Any, final: Any, expired_at: Any
) -> dict[str, Any]:
    if expired_at:
        return {
            "status": "expired",
            "suggested_reply": None,
            "final_reply": None,
            "diff": [],
        }
    if outcome in {"no_send_keep_watching", "no_send_end_task"}:
        return {
            "status": "not_applicable",
            "suggested_reply": suggested if isinstance(suggested, str) else None,
            "final_reply": None,
            "diff": [],
        }
    before = suggested if isinstance(suggested, str) else None
    after = final if isinstance(final, str) else None
    if before is None or after is None:
        return {
            "status": "unavailable",
            "suggested_reply": before,
            "final_reply": after,
            "diff": [],
        }
    changed = before != after
    return {
        "status": "changed" if changed else "unchanged",
        "suggested_reply": before,
        "final_reply": after,
        "diff": _text_diff(before, after) if changed else [],
    }


def _text_diff(before: str, after: str) -> list[dict[str, str]]:
    before_tokens = _DIFF_TOKEN_RE.findall(before)
    after_tokens = _DIFF_TOKEN_RE.findall(after)
    matcher = SequenceMatcher(a=before_tokens, b=after_tokens, autojunk=False)
    changes: list[dict[str, str]] = []
    for operation, i1, i2, j1, j2 in matcher.get_opcodes():
        item = {"op": operation}
        if operation in {"equal", "delete", "replace"}:
            item["before"] = "".join(before_tokens[i1:i2])
        if operation in {"equal", "insert", "replace"}:
            item["after"] = "".join(after_tokens[j1:j2])
        changes.append(item)
    return changes


def _count_dtos(rows: list[sqlite3.Row], *, total: int) -> list[dict[str, Any]]:
    return [
        {
            "value": str(row["value"]),
            "count": int(row["count"]),
            "rate": _ratio(int(row["count"]), total),
        }
        for row in rows
    ]


def _mode_filter(mode: FeedbackExecutionMode) -> tuple[str, list[str]]:
    return ("", []) if mode == "all" else ("AND execution_mode = ?", [mode])


def _execution_mode(value: str) -> FeedbackExecutionMode:
    if value not in {"production", "dry_run", "all"}:
        raise ValueError("execution_mode must be production, dry_run, or all")
    return value  # type: ignore[return-value]


def _days(value: int) -> int:
    if value < 1 or value > 3650:
        raise ValueError("feedback window days must be between 1 and 3650")
    return value


def _limit(value: int) -> int:
    if value < 1:
        raise ValueError("limit must be positive")
    return min(value, 100)


def _offset(value: int) -> int:
    if value < 0:
        raise ValueError("offset must not be negative")
    return value


def _since(now: str, days: int) -> str:
    try:
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperatorQueryReadError(f"invalid query clock: {now}") from exc
    return (parsed - timedelta(days=days)).isoformat(timespec="seconds")


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)
