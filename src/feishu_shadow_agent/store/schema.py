from __future__ import annotations

import sqlite3

SQLITE_APPLICATION_ID = 1179861319
SQLITE_SCHEMA_VERSION = 9


class IncompatibleSchemaError(RuntimeError):
    """The database is not the schema understood by this version."""


def has_schema_objects(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone())


def is_uninitialized_schema(conn: sqlite3.Connection) -> bool:
    return (
        not has_schema_objects(conn)
        and conn.execute("PRAGMA application_id").fetchone()[0] == 0
        and conn.execute("PRAGMA user_version").fetchone()[0] == 0
    )


def is_current_schema(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute("PRAGMA application_id").fetchone()[0] == SQLITE_APPLICATION_ID
        and conn.execute("PRAGMA user_version").fetchone()[0] == SQLITE_SCHEMA_VERSION
    )


def require_current_schema(conn: sqlite3.Connection) -> None:
    application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if application_id != SQLITE_APPLICATION_ID or version != SQLITE_SCHEMA_VERSION:
        raise IncompatibleSchemaError(
            "SQLite database is not the current schema baseline "
            f"(application_id={application_id}, version={version}; "
            f"expected application_id={SQLITE_APPLICATION_ID}, version={SQLITE_SCHEMA_VERSION}); "
            "configure an empty database"
        )
