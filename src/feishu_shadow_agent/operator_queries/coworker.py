from __future__ import annotations

import sqlite3
from typing import Any

from ..coworker_store import handoff_operator_command


def native_coworker_work(
    conn: sqlite3.Connection, *, limit: int, now: str, include_handoff_command: bool
) -> list[dict[str, Any]]:
    groups: dict[int | tuple[str, str, str], dict[str, Any]] = {}

    def group(row: sqlite3.Row) -> dict[str, Any]:
        session_id = row["session_id"]
        scope = (row["app_id"], row["chat_id"], row["topic_key"])
        return groups.setdefault(
            scope if session_id is None else session_id,
            {
                "session_id": session_id,
                "app_id": row["app_id"],
                "chat_id": row["chat_id"],
                "topic_key": row["topic_key"],
                "deliveries": [],
                "handoff": None,
                "watch_expires_at": None,
            },
        )

    # Filter unresolved work before limiting the list. Completed deliveries can
    # outnumber an older receipt that still needs an operator.
    for row in conn.execute(
        """SELECT s.id AS session_id, s.app_id, s.chat_id, s.thread_id AS topic_key,
                  d.id, d.purpose, d.target_message_id, d.sent_message_id,
                  d.thread_id AS receipt_thread_id, d.error, d.updated_at
           FROM coworker_native_deliveries d
           JOIN coworker_sessions s ON s.id = d.session_id
           WHERE d.status = 'needs_review'
           ORDER BY d.id DESC LIMIT ?""",
        (limit,),
    ):
        group(row)["deliveries"].append(
            {
                key: row[key]
                for key in (
                    "id",
                    "purpose",
                    "target_message_id",
                    "sent_message_id",
                    "receipt_thread_id",
                    "error",
                    "updated_at",
                )
            }
        )

    for row in conn.execute(
        """SELECT s.id AS session_id, s.app_id, s.chat_id, s.thread_id AS topic_key,
                  s.backend_session_id, s.agent_working_dir, s.state,
                  h.status, h.error, h.updated_at
           FROM coworker_native_handoffs h
           JOIN coworker_sessions s ON s.id = h.session_id
           ORDER BY h.updated_at DESC, h.session_id DESC LIMIT ?""",
        (limit,),
    ):
        group(row)["handoff"] = {
            "status": row["status"],
            "error": row["error"],
            "updated_at": row["updated_at"],
            "operator_command": handoff_operator_command(
                row["status"], row["agent_working_dir"], row["backend_session_id"]
            )
            if include_handoff_command and row["state"] == "active"
            else None,
        }

    for row in conn.execute(
        """SELECT s.id AS session_id, w.app_id, w.chat_id, w.topic_key, w.expires_at
           FROM coworker_native_watches w
           LEFT JOIN coworker_sessions s
               ON s.app_id = w.app_id AND s.chat_id = w.chat_id
               AND s.thread_id = w.topic_key AND s.provider = 'codex' AND s.state = 'active'
           WHERE julianday(w.expires_at) > julianday(?)
           ORDER BY w.updated_at DESC, w.app_id, w.chat_id, w.topic_key LIMIT ?""",
        (now, limit),
    ):
        group(row)["watch_expires_at"] = row["expires_at"]

    return list(groups.values())
