from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from feishu_shadow_agent.cli import main
from feishu_shadow_agent.config import AppConfig, OwnerConfig, RetentionConfig
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
    assert summary.resources_candidates == 3
    assert summary.resources_deleted == 1
    assert summary.resources_expired == 2
    assert [resource.reason for resource in summary.resources_skipped] == [
        "unsafe_path"
    ]
    assert not free_path.exists()
    assert active_path.exists()
    assert pending_path.exists()
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
    assert resources["om_free"]["file_key"] == "img_free"
    assert resources["om_missing"]["download_status"] == "expired"
    assert resources["om_missing"]["path"] is None
    assert resources["om_missing"]["sha256"] == "hash-img_missing"
    assert resources["om_missing"]["file_key"] == "img_missing"
    assert resources["om_active"]["download_status"] == "downloaded"
    assert resources["om_pending"]["download_status"] == "downloaded"
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


def test_feedback_retention_expires_content_then_deletes_metadata(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        retention=RetentionConfig(feedback_content_days=30, feedback_metadata_days=365),
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

    assert preview.feedback_content_candidates == 1
    assert preview.feedback_metadata_candidates == 1
    assert preview.feedback_content_expired == 0
    assert preview.feedback_metadata_deleted == 0
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

    assert applied.feedback_content_expired == 1
    assert applied.feedback_metadata_deleted == 1
    assert set(rows) == {"cmd_old", "cmd_recent"}
    assert rows["cmd_old"]["suggested_reply"] is None
    assert rows["cmd_old"]["final_reply"] is None
    assert rows["cmd_old"]["note"] is None
    assert rows["cmd_old"]["content_expired_at"] == NOW.isoformat(timespec="seconds")
    assert rows["cmd_old"]["decision_reason"] == "insufficient_evidence"
    assert rows["cmd_recent"]["suggested_reply"] == "suggested recent"
    assert rows["cmd_recent"]["content_expired_at"] is None


def test_feedback_metadata_null_keeps_row_after_content_expiry(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        retention=RetentionConfig(
            feedback_content_days=30, feedback_metadata_days=None
        ),
    )
    _insert_feedback(store, "ancient", created_at=ANCIENT)

    summary = RetentionService(store=store, config=config, base_dir=tmp_path).prune(
        now=NOW
    )

    with store.connect() as conn:
        feedback = conn.execute("SELECT * FROM approval_feedback").fetchone()
    assert summary.feedback_metadata_cutoff is None
    assert summary.feedback_metadata_deleted == 0
    assert summary.feedback_content_expired == 1
    assert feedback is not None
    assert feedback["suggested_reply"] is None
    assert feedback["decision_reason"] == "insufficient_evidence"


def test_daemon_retention_checkpoint_runs_at_most_daily(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")

    assert daemon_retention_is_due(store, now=NOW) is True

    summary = RetentionSummary(
        dry_run=False,
        raw_message_cutoff=OLD,
        resource_cutoff=OLD,
        feedback_content_cutoff=OLD,
        feedback_metadata_cutoff=OLD,
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
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, created_at, updated_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                short_id,
                status,
                "oc_1",
                root_message_id,
                "label",
                OLD,
                OLD,
                None if status == "watching" else OLD,
            ),
        )
    return int(cursor.lastrowid)


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
        conn.execute(
            """
            INSERT INTO approval_feedback(
              approval_id, task_id, command_id, outcome, decision_reason,
              suggested_reply, final_reply, feedback_reason, note, actor,
              execution_mode, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(approval.lastrowid),
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


def _write_resource(base_dir: Path, relative_path: str) -> Path:
    path = base_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"resource")
    return path
