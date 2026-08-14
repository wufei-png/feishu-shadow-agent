from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from feishu_shadow_agent.approval_cards import build_approval_card
from feishu_shadow_agent.card_actions import create_card_action_connection
from feishu_shadow_agent.config import ConfigService
from feishu_shadow_agent.feishu.lark_cli import LarkCliClient
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.paths import resolve_relative_path
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


@pytest.mark.skipif(
    os.environ.get("FEISHU_SHADOW_AGENT_REAL_E2E") != "1",
    reason="set FEISHU_SHADOW_AGENT_REAL_E2E=1 to send a real Feishu test message",
)
def test_real_approval_action_e2e() -> None:
    config_path = Path(os.environ.get("FEISHU_SHADOW_AGENT_CONFIG", "config.yaml"))
    approval_id = os.environ.get("FEISHU_SHADOW_AGENT_REAL_E2E_APPROVAL_ID", "")
    if not approval_id.startswith("a_"):
        pytest.fail(
            "FEISHU_SHADOW_AGENT_REAL_E2E_APPROVAL_ID must name one pending "
            "approval; this explicit target prevents accidental sends"
        )

    loaded = ConfigService().load(config_path)
    if not loaded.config.interactive_cards.enabled:
        pytest.fail("interactive_cards.enabled must be true in the selected config")
    store = SQLiteStore(
        resolve_relative_path(loaded.config.storage.sqlite_path, loaded.base_dir)
    )
    store.initialize()
    logger = JSONLLogger(
        resolve_relative_path(loaded.config.logging.jsonl_path, loaded.base_dir),
        level=loaded.config.logging.level,
        console=True,
    )

    with store.connect() as conn:
        approval = conn.execute(
            """
            SELECT a.id, a.short_id, a.task_id, a.payload_json, t.short_id AS task_short_id
            FROM approvals AS a
            LEFT JOIN tasks AS t ON t.id = a.task_id
            WHERE a.short_id = ? AND a.status = 'pending'
            """,
            (approval_id,),
        ).fetchone()
        if approval is None:
            pytest.fail(f"pending approval not found: {approval_id}")
        before_feedback = conn.execute(
            "SELECT COUNT(*) AS count FROM approval_feedback WHERE approval_id = ?",
            (approval["id"],),
        ).fetchone()["count"]

    payload = json.loads(approval["payload_json"] or "{}")
    suggested_reply = payload.get("text") or payload.get("composed_text")
    if not isinstance(suggested_reply, str) or not suggested_reply.strip():
        pytest.fail("selected approval does not contain a suggested reply")
    card = build_approval_card(
        {
            "type": "approval_required",
            "task_id": approval["task_short_id"] or "unknown",
            "approval_id": approval["short_id"],
            "reason": payload.get("decision_reason") or "real integration test",
            "suggested_reply": suggested_reply,
            "approvable": payload.get("approvable") is not False,
        }
    )

    connection = create_card_action_connection(
        store=store,
        config=loaded.config,
        logger=logger,
        wake=lambda: None,
        execution_mode="production",
    )
    try:
        if not connection.start():
            pytest.fail(f"callback connection is unhealthy: {connection.snapshot()}")
        send_result = LarkCliClient(
            path=loaded.config.lark_cli.path,
            timeout_seconds=loaded.config.lark_cli.timeout_seconds,
            cwd=loaded.base_dir,
        ).owner_card(
            owner_open_id=loaded.config.owner.open_id,
            card=card,
            idempotency_key=f"real-card-e2e:{approval_id}:{time.time_ns()}",
            dry_run=False,
        )
        if send_result.exit_code != 0:
            pytest.fail(f"real test message send failed: {send_result.error}")
        print(
            "Real approval message sent. Click one action in Feishu now; "
            "waiting for the local callback...",
            flush=True,
        )
        timeout_seconds = int(
            os.environ.get("FEISHU_SHADOW_AGENT_REAL_E2E_TIMEOUT", "180")
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with store.connect() as conn:
                feedback_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM approval_feedback WHERE approval_id = ?",
                    (approval["id"],),
                ).fetchone()["count"]
            if feedback_count > before_feedback:
                # Give the callback loop a moment to apply the immutable result
                # state before the finally block disconnects it.
                time.sleep(1)
                return
            time.sleep(1)
        pytest.fail(
            "no approval callback was recorded before timeout; "
            f"connection={connection.snapshot()}"
        )
    finally:
        connection.stop()
