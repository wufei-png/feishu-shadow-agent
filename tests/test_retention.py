from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from feishu_shadow_agent.cli import main
from feishu_shadow_agent.config import AppConfig, OwnerConfig, RetentionConfig
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.retention import (
    RAW_JSON_PRUNED_PLACEHOLDER,
    RetentionService,
    RetentionSummary,
    daemon_retention_is_due,
    record_daemon_retention_checkpoint,
)
from feishu_shadow_agent.store.sqlite_store import SQLiteStore

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
OLD = "2026-05-01T00:00:00+00:00"
RECENT = "2026-06-20T00:00:00+00:00"
ANCIENT = "2024-01-01T00:00:00+00:00"


def test_retention_prunes_raw_json_and_downloaded_resources(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner"))
    _insert_message(store, "om_old", OLD, raw={"message_id": "om_old", "raw": True})
    _insert_message(
        store, "om_recent", RECENT, raw={"message_id": "om_recent", "raw": True}
    )
    free_path = _write_resource(tmp_path, "data/resources/om_free/image.bin")
    active_path = _write_resource(tmp_path, "data/resources/om_active/image.bin")
    pending_path = _write_resource(tmp_path, "data/resources/om_pending/image.bin")
    _insert_resource(store, "om_free", "img_free", "data/resources/om_free/image.bin")
    _insert_resource(
        store, "om_missing", "img_missing", "data/resources/om_missing/image.bin"
    )
    _insert_resource(
        store, "om_active", "img_active", "data/resources/om_active/image.bin"
    )
    _insert_resource(
        store, "om_pending", "img_pending", "data/resources/om_pending/image.bin"
    )
    _insert_resource(store, "om_unsafe", "img_unsafe", "../outside.bin")
    _insert_task(store, "t_active", "watching", "om_active")
    pending_task_id = _insert_task(store, "t_pending", "closed", "om_pending")
    _insert_pending_approval(store, pending_task_id)

    summary = RetentionService(store=store, config=config, base_dir=tmp_path).prune(
        now=NOW
    )

    assert summary.raw_messages_pruned == 1
    assert summary.resources_candidates == 4
    assert summary.resources_deleted == 2
    assert summary.resources_expired == 3
    assert [resource.reason for resource in summary.resources_skipped] == [
        "unsafe_path"
    ]
    assert not free_path.exists()
    assert active_path.exists()
    assert not pending_path.exists()
    with store.connect() as conn:
        messages = {
            row["message_id"]: row["raw_json"]
            for row in conn.execute(
                "SELECT message_id, raw_json FROM messages ORDER BY message_id"
            )
        }
        resources = {
            row["message_id"]: dict(row)
            for row in conn.execute(
                "SELECT message_id, download_status, path, sha256, file_key FROM resources ORDER BY message_id"
            )
        }
    assert messages["om_old"] == RAW_JSON_PRUNED_PLACEHOLDER
    assert json.loads(messages["om_recent"])["raw"] is True
    assert resources["om_free"]["download_status"] == "expired"
    assert resources["om_free"]["path"] is None
    assert resources["om_free"]["sha256"] == "hash-img_free"
    assert resources["om_free"]["file_key"].startswith("retention-pruned:")
    assert resources["om_missing"]["download_status"] == "expired"
    assert resources["om_missing"]["path"] is None
    assert resources["om_missing"]["sha256"] == "hash-img_missing"
    assert resources["om_missing"]["file_key"].startswith("retention-pruned:")
    assert resources["om_active"]["download_status"] == "downloaded"
    assert resources["om_active"]["file_key"] == "img_active"
    assert resources["om_pending"]["download_status"] == "expired"
    assert resources["om_unsafe"]["download_status"] == "downloaded"


def test_retention_cli_dry_run_previews_without_mutating_then_prunes(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
storage:
  sqlite_path: agent.sqlite3
  resource_dir: data/resources
logging:
  jsonl_path: agent.jsonl
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    _insert_message(store, "om_old", OLD, raw={"message_id": "om_old", "raw": True})
    resource_path = _write_resource(tmp_path, "data/resources/om_cli/image.bin")
    _insert_resource(store, "om_cli", "img_cli", "data/resources/om_cli/image.bin")

    assert main(["retention", "prune", "--config", str(config_path), "--dry-run"]) == 0

    preview = yaml.safe_load(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["raw_messages_pruned"] == 1
    assert preview["resources_candidates"] == 1
    assert preview["resources_deleted"] == 0
    assert resource_path.exists()
    with store.connect() as conn:
        message = conn.execute(
            "SELECT raw_json FROM messages WHERE message_id = ?", ("om_old",)
        ).fetchone()
        resource = conn.execute(
            "SELECT download_status FROM resources WHERE message_id = ?", ("om_cli",)
        ).fetchone()
    assert json.loads(message["raw_json"])["raw"] is True
    assert resource["download_status"] == "downloaded"

    assert main(["retention", "prune", "--config", str(config_path)]) == 0

    applied = yaml.safe_load(capsys.readouterr().out)
    assert applied["dry_run"] is False
    assert applied["raw_messages_pruned"] == 1
    assert applied["resources_deleted"] == 1
    assert not resource_path.exists()
    with store.connect() as conn:
        message = conn.execute(
            "SELECT raw_json FROM messages WHERE message_id = ?", ("om_old",)
        ).fetchone()
        resource = conn.execute(
            "SELECT download_status, path FROM resources WHERE message_id = ?",
            ("om_cli",),
        ).fetchone()
    assert message["raw_json"] == RAW_JSON_PRUNED_PLACEHOLDER
    assert resource["download_status"] == "expired"
    assert resource["path"] is None


def test_feedback_retention_scrubs_content_and_preserves_audit_metadata(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        retention=RetentionConfig(feedback_content_days=30),
    )
    _insert_feedback(store, "old", created_at=OLD)
    _insert_feedback(store, "ancient", created_at=ANCIENT)
    _insert_feedback(store, "recent", created_at=RECENT)
    service = RetentionService(store=store, config=config, base_dir=tmp_path)

    preview = service.prune(now=NOW, dry_run=True)
    with store.connect() as conn:
        preview_rows = conn.execute(
            "SELECT suggested_reply, content_expired_at FROM approval_feedback"
        ).fetchall()

    assert preview.feedback_content_candidates == 2
    assert preview.feedback_content_expired == 0
    assert all(row["suggested_reply"] is not None for row in preview_rows)
    assert all(row["content_expired_at"] is None for row in preview_rows)

    applied = service.prune(now=NOW)
    with store.connect() as conn:
        rows = {
            row["command_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM approval_feedback ORDER BY command_id"
            ).fetchall()
        }

    assert applied.feedback_content_expired == 2
    assert set(rows) == {"cmd_ancient", "cmd_old", "cmd_recent"}
    assert rows["cmd_old"]["suggested_reply"] is None
    assert rows["cmd_old"]["final_reply"] is None
    assert rows["cmd_old"]["note"] is None
    assert rows["cmd_old"]["content_expired_at"] == NOW.isoformat(timespec="seconds")
    assert rows["cmd_old"]["decision_reason"] == "insufficient_evidence"
    assert rows["cmd_ancient"]["suggested_reply"] is None
    assert rows["cmd_ancient"]["decision_reason"] == "insufficient_evidence"
    assert rows["cmd_recent"]["suggested_reply"] == "suggested recent"
    assert rows["cmd_recent"]["content_expired_at"] is None


def test_feedback_audit_row_is_always_kept_after_content_expiry(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        retention=RetentionConfig(feedback_content_days=30),
    )
    _insert_feedback(store, "ancient", created_at=ANCIENT)

    summary = RetentionService(store=store, config=config, base_dir=tmp_path).prune(
        now=NOW
    )

    with store.connect() as conn:
        feedback = conn.execute("SELECT * FROM approval_feedback").fetchone()
    assert summary.feedback_content_expired == 1
    assert feedback is not None
    assert feedback["suggested_reply"] is None
    assert feedback["decision_reason"] == "insufficient_evidence"


def test_retention_scrubs_full_chain_except_effective_watch(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    _insert_sensitive_chain(
        store,
        "closed",
        status="closed",
        watch_until=None,
        created_at=OLD,
    )
    _insert_sensitive_chain(
        store,
        "active",
        status="watching",
        watch_until="2026-07-01T00:00:00+00:00",
        created_at=OLD,
    )
    _insert_sensitive_chain(
        store,
        "expired_watch",
        status="watching",
        watch_until="2026-06-01T00:00:00+00:00",
        created_at=OLD,
    )
    _insert_sensitive_chain(
        store,
        "recent",
        status="closed",
        watch_until=None,
        created_at=RECENT,
    )
    service = RetentionService(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        base_dir=tmp_path,
    )

    preview = service.prune(now=NOW, dry_run=True)
    applied = service.prune(now=NOW)

    expected = {
        "messages",
        "tasks",
        "task_watch_keys",
        "approvals",
        "actions",
        "dispatch_attempts",
        "resources",
        "agent_audits",
        "approval_commands",
        "approval_feedback",
        "message_processing",
    }
    assert set(preview.content_candidates) == expected
    assert set(applied.content_scrubbed) == expected
    assert all(count == 2 for count in preview.content_candidates.values())
    assert all(count == 2 for count in applied.content_scrubbed.values())

    with store.connect() as conn:
        messages = {
            row["message_id"]: dict(row)
            for row in conn.execute("SELECT * FROM messages ORDER BY message_id")
        }
        tasks = {
            row["short_id"]: dict(row)
            for row in conn.execute("SELECT * FROM tasks ORDER BY short_id")
        }
        approvals = {
            row["short_id"]: dict(row)
            for row in conn.execute("SELECT * FROM approvals ORDER BY short_id")
        }
        actions = {
            row["idempotency_key"]: dict(row)
            for row in conn.execute("SELECT * FROM actions ORDER BY idempotency_key")
        }
        resources = {
            row["message_id"]: dict(row)
            for row in conn.execute("SELECT * FROM resources ORDER BY message_id")
        }
        audits = {
            row["task_id"]: dict(row)
            for row in conn.execute("SELECT * FROM agent_audits ORDER BY task_id")
        }
        commands = {
            row["message_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM approval_commands ORDER BY message_id"
            )
        }
        feedback = {
            row["command_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM approval_feedback ORDER BY command_id"
            )
        }
        processing = {
            row["message_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM message_processing ORDER BY message_id"
            )
        }
        watch_keys = {
            row["task_id"]: row["key"]
            for row in conn.execute("SELECT * FROM task_watch_keys ORDER BY task_id")
        }

    for suffix in ("closed", "expired_watch"):
        message = messages[f"om_{suffix}"]
        assert message["text"] is None
        assert message["raw_json"] == RAW_JSON_PRUNED_PLACEHOLDER
        task = tasks[f"t_{suffix}"]
        assert task["task_label"] is None
        assert task["agent_session_id"] is None
        assert task["status"] in {"closed", "watching"}
        assert approvals[f"a_{suffix}"]["preview"] is None
        assert (
            actions[f"action_{suffix}"]["payload_json"] == RAW_JSON_PRUNED_PLACEHOLDER
        )
        assert resources[f"om_{suffix}"]["file_key"].startswith("retention-pruned:")
        assert audits[task["id"]]["prompt_json"] is None
        assert audits[task["id"]]["input_resource_ids_json"] == "[]"
        assert commands[f"cmd_{suffix}"]["command"] == "[retention_pruned]"
        assert feedback[f"cmd_{suffix}"]["suggested_reply"] is None
        assert processing[f"om_{suffix}"]["last_error"] is None
        assert watch_keys[task["id"]].startswith("retention-pruned:")

    active = tasks["t_active"]
    assert messages["om_active"]["text"] == "secret active"
    assert active["task_label"] == "secret label active"
    assert approvals["a_active"]["preview"] == "secret preview active"
    assert commands["cmd_active"]["command"] == "/approve secret active"
    assert feedback["cmd_active"]["suggested_reply"] == "secret suggested active"
    assert processing["om_active"]["last_error"] == "secret error active"
    assert watch_keys[active["id"]] == "secret-key-active"

    recent = tasks["t_recent"]
    assert messages["om_recent"]["text"] == "secret recent"
    assert recent["task_label"] == "secret label recent"
    assert len(messages) == len(tasks) == len(approvals) == len(actions) == 4
    assert len(resources) == len(audits) == len(commands) == len(feedback) == 4
    assert len(processing) == len(watch_keys) == 4


def test_retention_scrubs_jsonl_and_text_log_payloads_atomically(
    tmp_path: Path,
) -> None:
    jsonl_path = tmp_path / "agent.jsonl"
    text_path = tmp_path / "agent.log"
    jsonl_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": OLD,
                        "level": "info",
                        "run_id": "run_old",
                        "task_id": "t_old",
                        "event": "old_event",
                        "data": {"secret": "old"},
                    }
                ),
                json.dumps(
                    {
                        "ts": RECENT,
                        "level": "info",
                        "run_id": "run_recent",
                        "task_id": "t_recent",
                        "event": "recent_event",
                        "data": {"secret": "recent"},
                    }
                ),
                json.dumps(
                    {
                        "ts": OLD,
                        "level": "info",
                        "run_id": "run_active",
                        "task_id": "t_active",
                        "event": "active_event",
                        "data": {"secret": "active"},
                    }
                ),
                json.dumps(
                    {
                        "ts": OLD,
                        "level": "info",
                        "run_id": "run_active_resource",
                        "task_id": None,
                        "event": "resource_downloaded",
                        "data": {
                            "message_id": "om_active",
                            "secret": "active resource",
                        },
                    }
                ),
                json.dumps(
                    {
                        "ts": OLD,
                        "level": "info",
                        "run_id": "run_active_action",
                        "task_id": None,
                        "event": "dispatch_action_completed",
                        "data": {"action_id": 1, "secret": "active action"},
                    }
                ),
                json.dumps(
                    {
                        "ts": OLD,
                        "level": "info",
                        "run_id": "run_active_approval",
                        "task_id": None,
                        "event": "card_action_processed",
                        "data": {
                            "approval_id": "a_active",
                            "secret": "active approval",
                        },
                    }
                ),
                "malformed secret line",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    text_path.write_text(
        f"{OLD} info old_event secret=old\n"
        f"{RECENT} info recent_event secret=recent\n"
        f"{OLD} info active_event task_id=t_active secret=active\n"
        f"{OLD} info resource_downloaded message_id=om_active secret=active-resource\n"
        f"{OLD} info dispatch_action_completed action_id=1 secret=active-action\n"
        f"{OLD} info card_action_processed approval_id=a_active secret=active-approval\n"
        "malformed secret line\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    active_task_id = _insert_task(store, "t_active", "watching", "om_active")
    with store.connect() as conn:
        approval = conn.execute(
            """
            INSERT INTO approvals(
              short_id, task_id, kind, status, payload_json, created_at
            ) VALUES ('a_active', ?, 'send_reply', 'pending', '{}', ?)
            """,
            (active_task_id, OLD),
        )
        assert approval.lastrowid is not None
        conn.execute(
            """
            INSERT INTO actions(
              idempotency_key, task_id, approval_id, kind, status,
              dry_run, execution_mode, payload_json, created_at, updated_at
            ) VALUES ('action_active', ?, ?, 'send_reply', 'pending',
                      0, 'production', '{}', ?, ?)
            """,
            (active_task_id, approval.lastrowid, OLD, OLD),
        )
    logger = JSONLLogger(jsonl_path, text_path=text_path)
    service = RetentionService(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="ou_owner")),
        base_dir=tmp_path,
        logger=logger,
    )

    preview = service.prune(now=NOW, dry_run=True)

    assert preview.log_content_candidates == {"jsonl": 2, "text": 2}
    assert "secret=old" in text_path.read_text(encoding="utf-8")

    applied = service.prune(now=NOW)

    assert applied.log_content_scrubbed == {"jsonl": 2, "text": 2}
    json_rows = [
        json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert json_rows[0]["data"] == {"retention_pruned": True}
    assert json_rows[1]["data"] == {"secret": "recent"}
    assert json_rows[2]["data"] == {"secret": "active"}
    assert json_rows[3]["data"]["secret"] == "active resource"
    assert json_rows[4]["data"]["secret"] == "active action"
    assert json_rows[5]["data"]["secret"] == "active approval"
    assert json_rows[6]["event"] == "retention_unparseable_line_pruned"
    text = text_path.read_text(encoding="utf-8")
    assert "secret=old" not in text
    assert "secret=recent" in text
    assert "secret=active" in text
    assert "secret=active-resource" in text
    assert "secret=active-action" in text
    assert "secret=active-approval" in text
    assert "malformed secret line" not in text
    assert jsonl_path.stat().st_mode & 0o777 == 0o600
    assert text_path.stat().st_mode & 0o777 == 0o600

    second = service.prune(now=NOW)
    assert second.log_content_candidates == {"jsonl": 0, "text": 0}


def test_daemon_retention_checkpoint_runs_at_most_daily(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")

    assert daemon_retention_is_due(store, now=NOW) is True

    summary = RetentionSummary(
        dry_run=False,
        raw_message_cutoff=OLD,
        resource_cutoff=OLD,
        feedback_content_cutoff=OLD,
    )
    record_daemon_retention_checkpoint(store, summary=summary, now=NOW)

    assert (
        daemon_retention_is_due(store, now=NOW + timedelta(hours=23, minutes=59))
        is False
    )
    assert daemon_retention_is_due(store, now=NOW + timedelta(hours=24)) is True


def _insert_message(
    store: SQLiteStore, message_id: str, inserted_at: str, *, raw: dict[str, object]
) -> None:
    store.initialize()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO messages(message_id, chat_id, chat_type, sender_id, sent_at, text, raw_json, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                "oc_1",
                "p2p",
                "ou_a",
                inserted_at,
                "hello",
                json.dumps(raw, ensure_ascii=False),
                inserted_at,
            ),
        )


def _insert_resource(
    store: SQLiteStore, message_id: str, file_key: str, path: str
) -> None:
    store.initialize()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO resources(
              message_id, file_key, resource_type, download_status, path, sha256, raw_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                file_key,
                "image",
                "downloaded",
                path,
                f"hash-{file_key}",
                "{}",
                OLD,
                OLD,
            ),
        )


def _insert_task(
    store: SQLiteStore, short_id: str, status: str, root_message_id: str
) -> int:
    store.initialize()
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks(
              short_id, status, chat_id, root_message_id, task_label,
              watch_until, created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                short_id,
                status,
                "oc_1",
                root_message_id,
                "label",
                "2026-07-01T00:00:00+00:00" if status == "watching" else None,
                OLD,
                OLD,
                None if status == "watching" else OLD,
            ),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_pending_approval(store: SQLiteStore, task_id: int) -> None:
    store.initialize()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO approvals(short_id, task_id, kind, status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("a_pending", task_id, "send_reply", "pending", "{}", OLD),
        )


def _insert_feedback(store: SQLiteStore, suffix: str, *, created_at: str) -> None:
    task_id = _insert_task(store, f"t_{suffix}", "closed", f"om_{suffix}")
    with store.connect() as conn:
        approval = conn.execute(
            """
            INSERT INTO approvals(
              short_id, task_id, kind, status, payload_json, preview, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"a_{suffix}",
                task_id,
                "send_reply",
                "approved",
                "{}",
                f"suggested {suffix}",
                created_at,
                created_at,
            ),
        )
        assert approval.lastrowid is not None
        conn.execute(
            """
            INSERT INTO approval_feedback(
              approval_id, task_id, command_id, outcome, decision_reason,
              suggested_reply, final_reply, feedback_reason, note, actor,
              execution_mode, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval.lastrowid,
                task_id,
                f"cmd_{suffix}",
                "edited_sent",
                "insufficient_evidence",
                f"suggested {suffix}",
                f"final {suffix}",
                "tone_or_style",
                f"note {suffix}",
                "owner",
                "production",
                created_at,
            ),
        )


def _insert_sensitive_chain(
    store: SQLiteStore,
    suffix: str,
    *,
    status: str,
    watch_until: str | None,
    created_at: str,
) -> None:
    store.initialize()
    message_id = f"om_{suffix}"
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO messages(
              message_id, chat_id, sender_name, sent_at, text,
              normalized_json, raw_json, inserted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                "oc_1",
                f"secret sender {suffix}",
                created_at,
                f"secret {suffix}",
                json.dumps({"text": f"secret {suffix}"}),
                json.dumps({"raw": f"secret {suffix}"}),
                created_at,
            ),
        )
        task = conn.execute(
            """
            INSERT INTO tasks(
              short_id, status, chat_id, root_message_id, task_label,
              agent_session_id, agent_session_provider, agent_working_dir,
              watch_until, last_user_message, last_agent_reply,
              created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"t_{suffix}",
                status,
                "oc_1",
                message_id,
                f"secret label {suffix}",
                f"secret-session-{suffix}",
                "test",
                f"/secret/{suffix}",
                watch_until,
                f"secret user {suffix}",
                f"secret reply {suffix}",
                created_at,
                created_at,
                created_at if status == "closed" else None,
            ),
        )
        assert task.lastrowid is not None
        task_id = task.lastrowid
        conn.execute(
            "INSERT INTO task_messages(task_id, message_id, role, created_at) VALUES (?, ?, ?, ?)",
            (task_id, message_id, "trigger", created_at),
        )
        conn.execute(
            "INSERT INTO task_watch_keys(task_id, key, created_at) VALUES (?, ?, ?)",
            (task_id, f"secret-key-{suffix}", created_at),
        )
        approval = conn.execute(
            """
            INSERT INTO approvals(
              short_id, task_id, kind, status, payload_json, preview,
              created_at, resolved_at
            ) VALUES (?, ?, 'send_reply', 'approved', ?, ?, ?, ?)
            """,
            (
                f"a_{suffix}",
                task_id,
                json.dumps({"text": f"secret payload {suffix}"}),
                f"secret preview {suffix}",
                created_at,
                created_at,
            ),
        )
        assert approval.lastrowid is not None
        approval_id = approval.lastrowid
        action = conn.execute(
            """
            INSERT INTO actions(
              idempotency_key, task_id, approval_id, kind, status,
              target_message_id, dry_run, execution_mode, payload_json,
              result_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'send_reply', 'sent', ?, 0, 'production', ?, ?, ?, ?)
            """,
            (
                f"action_{suffix}",
                task_id,
                approval_id,
                message_id,
                json.dumps({"text": f"secret action {suffix}"}),
                json.dumps({"reply": f"secret result {suffix}"}),
                created_at,
                created_at,
            ),
        )
        assert action.lastrowid is not None
        conn.execute(
            """
            INSERT INTO dispatch_attempts(
              action_id, claim_token, status, dry_run_result_json,
              send_result_json, readback_result_json, started_at, finished_at
            ) VALUES (?, ?, 'readback_ok', ?, ?, ?, ?, ?)
            """,
            (
                action.lastrowid,
                f"claim_{suffix}",
                json.dumps({"secret": suffix}),
                json.dumps({"secret": suffix}),
                json.dumps({"secret": suffix}),
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO resources(
              message_id, file_key, resource_type, download_status,
              raw_json, created_at, updated_at
            ) VALUES (?, ?, 'file', 'failed', ?, ?, ?)
            """,
            (
                message_id,
                f"secret-file-key-{suffix}",
                json.dumps({"secret": suffix}),
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_audits(
              backend_provider, request_type, task_id, agent_session_id,
              input_resource_ids_json, response_json, error, prompt_json, created_at
            ) VALUES ('test', 'task_session', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                f"secret-session-{suffix}",
                json.dumps([f"secret-file-key-{suffix}"]),
                json.dumps({"secret": suffix}),
                f"secret agent error {suffix}",
                json.dumps({"secret": suffix}),
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO approval_commands(
              message_id, command, status, result_json, created_at, updated_at
            ) VALUES (?, ?, 'applied', ?, ?, ?)
            """,
            (
                f"cmd_{suffix}",
                f"/approve secret {suffix}",
                json.dumps({"secret": suffix}),
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO approval_feedback(
              approval_id, task_id, command_id, outcome, decision_reason,
              suggested_reply, final_reply, feedback_reason, note, actor,
              execution_mode, created_at
            ) VALUES (?, ?, ?, 'suggestion_sent', 'supported', ?, ?, 'other', ?,
                      'owner', 'production', ?)
            """,
            (
                approval_id,
                task_id,
                f"cmd_{suffix}",
                f"secret suggested {suffix}",
                f"secret final {suffix}",
                f"secret note {suffix}",
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO message_processing(
              message_id, task_id, stage, status, attempt_count, last_error,
              terminal_reason, created_at, updated_at
            ) VALUES (?, ?, 'task_session', 'processing_failed_terminal', 3, ?,
                      'attempts_exhausted', ?, ?)
            """,
            (message_id, task_id, f"secret error {suffix}", created_at, created_at),
        )


def _write_resource(base_dir: Path, relative_path: str) -> Path:
    path = base_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"resource")
    return path
