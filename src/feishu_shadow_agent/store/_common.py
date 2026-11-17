from __future__ import annotations

import json
import sqlite3
from typing import Any, cast


def row_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    keys = row.keys()
    return {key: row[key] for key in keys}


def json_row_dict(row: sqlite3.Row | dict[str, Any], *columns: str) -> dict[str, Any]:
    data = row_dict(row) or {}
    for column in columns:
        if column in data:
            data[column] = loads_json(data[column])
    return data


def loads_json(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def cursor_lastrowid(cursor: sqlite3.Cursor) -> int:
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        raise RuntimeError("SQLite insert did not return a row id")
    return int(lastrowid)


def loads_json_object(value: Any) -> dict[str, Any]:
    loaded = loads_json(value)
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
