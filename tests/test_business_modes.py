from __future__ import annotations

import json
from pathlib import Path

from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    CoworkerConfig,
    OwnerConfig,
)
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.membership import _record_observation
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _pending_action(store: SQLiteStore, *, key: str, source: str) -> int:
    store.initialize()
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO actions(idempotency_key, kind, status, payload_json, created_at, updated_at)
            VALUES (?, 'send_reply', 'pending', ?, ?, ?)
            """,
            (
                key,
                json.dumps({"source": source}),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def test_dispatch_scope_filters_before_limit_and_keeps_other_mode_pending(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    shadow = _pending_action(store, key="shadow", source="auto_reply")
    coworker = _pending_action(store, key="coworker", source="coworker")
    assert [a.id for a in store.list_dispatchable_actions(scope="shadow", limit=1)] == [
        shadow
    ]
    assert [
        a.id for a in store.list_dispatchable_actions(scope="coworker", limit=1)
    ] == [coworker]
    assert {a.id for a in store.list_dispatchable_actions()} == {shadow, coworker}


def test_explicit_business_origin_takes_priority_over_legacy_source(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    coworker = _pending_action(store, key="coworker", source="auto_reply")
    shadow = _pending_action(store, key="shadow", source="coworker_old")
    with store.connect() as conn:
        conn.execute(
            "UPDATE actions SET payload_json=? WHERE id=?",
            (
                json.dumps({"source": "auto_reply", "business_mode": "coworker"}),
                coworker,
            ),
        )
        conn.execute(
            "UPDATE actions SET payload_json=? WHERE id=?",
            (json.dumps({"source": "coworker_old", "business_mode": "shadow"}), shadow),
        )
    assert [
        action.id for action in store.list_dispatchable_actions(scope="coworker")
    ] == [coworker]
    assert [
        action.id for action in store.list_dispatchable_actions(scope="shadow")
    ] == [shadow]


def test_approval_expiry_and_commands_are_limited_to_active_mode(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.initialize()
    with store.connect() as conn:
        for short_id, source in [
            ("a_shadow", "approval_request"),
            ("a_coworker", "coworker_approval"),
        ]:
            conn.execute(
                """
                INSERT INTO approvals(short_id, kind, status, payload_json, created_at, expires_at)
                VALUES (?, 'send_reply', 'pending', ?, ?, ?)
                """,
                (
                    short_id,
                    json.dumps({"source": source}),
                    "2020-01-01T00:00:00Z",
                    "2020-01-02T00:00:00Z",
                ),
            )
    ignored = store.apply_approval_command(
        message_id="command_1",
        command="/approve a_shadow",
        verb="approve",
        target_id="a_shadow",
        scope="coworker",
    )
    assert ignored["status"] == "ignored_scope"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approval_commands").fetchone()[0] == 0
    assert (
        store.expire_pending_approvals(now="2026-01-01T00:00:00Z", scope="coworker")
        == 1
    )
    with store.connect() as conn:
        statuses = dict(
            conn.execute("SELECT short_id, status FROM approvals").fetchall()
        )
    assert statuses == {"a_shadow": "pending", "a_coworker": "expired"}
    assert (
        store.expire_pending_approvals(now="2026-01-01T00:00:00Z", scope="shadow") == 1
    )


def test_coworker_approval_notification_and_stale_recovery_stay_in_scope(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    shadow = _pending_action(store, key="shadow", source="auto_reply")
    coworker = _pending_action(store, key="coworker", source="coworker")
    with store.connect() as conn:
        conn.execute(
            "UPDATE actions SET status='sending' WHERE id IN (?, ?)", (shadow, coworker)
        )
        approval = conn.execute(
            """
            INSERT INTO approvals(short_id, kind, status, payload_json, created_at)
            VALUES ('a_coworker', 'send_reply', 'pending', ?, ?)
            """,
            (json.dumps({"source": "coworker_approval"}), "2020-01-01T00:00:00Z"),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO actions(idempotency_key, approval_id, kind, status, payload_json, created_at, updated_at)
            VALUES ('notify', ?, 'owner_notification', 'pending', '{}', ?, ?)
            """,
            (approval, "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"),
        )
    assert (
        len(
            store.list_dispatchable_actions(scope="coworker", kind="owner_notification")
        )
        == 1
    )
    assert (
        store.list_dispatchable_actions(scope="shadow", kind="owner_notification") == []
    )
    recovered = store.mark_stale_sending_actions_failed_needs_review(
        now="2026-11-18T00:00:00Z", scope="coworker"
    )
    assert [row["action_id"] for row in recovered] == [coworker]
    with store.connect() as conn:
        assert (
            conn.execute("SELECT status FROM actions WHERE id=?", (shadow,)).fetchone()[
                0
            ]
            == "sending"
        )


def test_coworker_membership_notification_keeps_its_business_scope(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="owner"),
        coworker=CoworkerConfig(enabled=True, chat_id="group"),
        chats={"group": ChatPolicyConfig(bot_joined=True)},
    )
    store.import_product_policy_from_config(config)
    assert _record_observation(
        store=store,
        config=config,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        execution_mode="production",
        business_mode="coworker",
        chat_id="group",
        status="absent",
        checked_at="2026-11-18T00:00:00Z",
        source="active_probe",
        error=None,
        run_id="test",
    )
    assert len(store.list_dispatchable_actions(scope="coworker")) == 1
    assert store.list_dispatchable_actions(scope="shadow") == []
