from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from .message_normalization import MessageNormalizer, mention_details
from .time_utils import parse_instant_or_none


def read_cached_history(
    database: Path,
    *,
    app_id: str,
    chat_id: str,
    topic_key: str | None = None,
    query: str | None = None,
    before_event_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Read one bounded page without changing the live store or fetching Feishu."""
    if not app_id or not chat_id or not 1 <= limit <= 100:
        raise ValueError("app, chat, and a limit between 1 and 100 are required")
    if before_event_id is not None and before_event_id < 1:
        raise ValueError("before-event-id must be positive")
    if (since is not None and parse_instant_or_none(since) is None) or (
        until is not None and parse_instant_or_none(until) is None
    ):
        raise ValueError("since and until must be timestamps with timezone offsets")
    if since and until:
        start = parse_instant_or_none(since)
        end = parse_instant_or_none(until)
        if start is not None and end is not None and start >= end:
            raise ValueError("since must precede until")
    escaped = (
        None
        if not query
        else query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    uri = database.expanduser().resolve(strict=True).as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        before_time = None
        if before_event_id is not None:
            cursor = conn.execute(
                """
                SELECT COALESCE(julianday(m.sent_at), julianday(e.received_at), 0) AS ordering_time
                FROM coworker_events e JOIN messages m ON m.message_id = e.message_id
                WHERE e.id = ? AND e.app_id = ? AND e.chat_id = ?
                  AND (? IS NULL OR e.topic_key = ?)
                """,
                (before_event_id, app_id, chat_id, topic_key, topic_key),
            ).fetchone()
            if cursor is None:
                raise ValueError("history cursor is unavailable in this scope")
            before_time = cursor["ordering_time"]
        rows = conn.execute(
            """
            SELECT e.id AS event_id, e.topic_key, m.* FROM coworker_events e
            JOIN messages m ON m.message_id = e.message_id
            WHERE e.app_id = ? AND e.chat_id = ?
              AND (? IS NULL OR e.topic_key = ?)
              AND (? IS NULL OR COALESCE(julianday(m.sent_at), julianday(e.received_at), 0) < ?
                   OR (COALESCE(julianday(m.sent_at), julianday(e.received_at), 0) = ? AND e.id < ?))
              AND (? IS NULL OR COALESCE(m.text, '') LIKE '%' || ? || '%' ESCAPE '\\')
              AND (? IS NULL OR COALESCE(julianday(m.sent_at), julianday(e.received_at), 0) >= julianday(?))
              AND (? IS NULL OR COALESCE(julianday(m.sent_at), julianday(e.received_at), 0) < julianday(?))
            ORDER BY COALESCE(julianday(m.sent_at), julianday(e.received_at), 0) DESC, e.id DESC LIMIT ?
            """,
            (
                app_id,
                chat_id,
                topic_key,
                topic_key,
                before_event_id,
                before_time,
                before_time,
                before_event_id,
                escaped,
                escaped,
                since,
                since,
                until,
                until,
                limit + 1,
            ),
        ).fetchall()
        states = conn.execute(
            """
            SELECT topic_key, needs_backfill, has_more, last_error, updated_at
            FROM coworker_history_state
            WHERE app_id = ? AND chat_id = ? AND (? IS NULL OR topic_key = ?)
            ORDER BY topic_key
            """,
            (app_id, chat_id, topic_key, topic_key),
        ).fetchall()
    page = rows[:limit]
    return {
        "source": "local_cache",
        "coverage": "received_or_backfilled_messages_only",
        "ordering": "sent_at_or_received_at_when_missing",
        "history_state": [dict(row) for row in states],
        "messages": [_cached_message(row) for row in reversed(page)],
        "has_more": len(rows) > limit,
        "next_before_event_id": int(page[-1]["event_id"])
        if len(rows) > limit
        else None,
    }


def _cached_message(row: sqlite3.Row) -> dict[str, Any]:
    result: dict[str, Any] = {
        key: row[key]
        for key in (
            "message_id",
            "topic_key",
            "sender_id",
            "sender_name",
            "sender_type",
            "sent_at",
            "reply_to_message_id",
            "text",
            "message_type",
            "is_deleted",
        )
    }
    try:
        parsed: Any = json.loads(str(row["raw_json"] or "{}"))
        if not isinstance(parsed, dict):
            raise ValueError("cached message content is unavailable")
        raw = cast(dict[str, Any], parsed)
        if not raw.get("message_id"):
            raise ValueError("cached message content is unavailable")
        normalized = MessageNormalizer(owner_open_id="").normalize(raw)
        mentions = mention_details(raw.get("mention_details")) or mention_details(
            raw.get("mentions")
        )
        raw_mentions = raw.get("mentions")
        if not mentions and isinstance(raw_mentions, list):
            mentions = [
                {"open_id": item}
                for item in cast(list[object], raw_mentions)
                if isinstance(item, str) and item
            ]
        result["mentions"] = mentions
        result["mention_mapping_available"] = (
            all("key" in item for item in mentions)
            if mentions
            else True
            if isinstance(raw_mentions, list)
            else None
        )
        result["resources"] = [
            {"type": resource.resource_type, "file_key": resource.file_key}
            for resource in normalized.resources
        ]
    except (ValueError, TypeError, json.JSONDecodeError):
        result["resources"] = []
        result["resources_unavailable"] = True
        result["mentions"] = []
        result["mention_mapping_available"] = None
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read the coworker's local message cache"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--topic-key")
    parser.add_argument("--query")
    parser.add_argument("--before-event-id", type=int)
    parser.add_argument("--since", help="Inclusive timestamp with timezone")
    parser.add_argument("--until", help="Exclusive timestamp with timezone")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    try:
        result = read_cached_history(
            args.database,
            app_id=args.app_id,
            chat_id=args.chat_id,
            topic_key=args.topic_key,
            query=args.query,
            before_event_id=args.before_event_id,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"cache read failed: {type(exc).__name__}\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
