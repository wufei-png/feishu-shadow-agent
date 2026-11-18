from __future__ import annotations

from pathlib import Path

import pytest

from feishu_shadow_agent.cli import _load_runtime, main
from feishu_shadow_agent.config import ConfigError, ConfigService
from feishu_shadow_agent.runtime_mode import dry_run_config, sync_production_policy
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        """owner:
  open_id: ou_owner
storage:
  sqlite_path: data/agent.sqlite3
  resource_dir: data/resources
logging:
  jsonl_path: logs/agent.jsonl
  console: false
chats:
  oc_one:
    auto_reply: true
""",
        encoding="utf-8",
    )
    return path


def test_dry_run_snapshots_only_current_policy_and_sync_is_explicit(
    tmp_path: Path,
) -> None:
    path = _config(tmp_path)
    production = ConfigService().load(path)
    prod_store = SQLiteStore(tmp_path / "data/agent.sqlite3")
    prod_store.import_product_policy_from_config(production.config)
    prod_store.upsert_chat_product_policy(
        {"chat_id": "oc_one", "auto_reply": False}, actor="test"
    )
    with prod_store.connect() as conn:
        conn.execute(
            "INSERT INTO messages(message_id, chat_id, chat_type, raw_json, inserted_at) VALUES (?, ?, ?, ?, ?)",
            ("om_private", "oc_one", "group", "{}", "now"),
        )

    loaded, dry_store, logger = _load_runtime(str(path), mode="dry-run")
    logger.info("dry_run_test")
    assert dry_store.path != prod_store.path
    assert loaded.config.storage.resource_dir == "data/resources.dry-run"
    assert (tmp_path / "logs/agent.jsonl.dry-run").is_file()
    assert not (tmp_path / "logs/agent.jsonl").exists()
    dry_policy = dry_store.get_chat_product_policy("oc_one")
    assert dry_policy is not None and dry_policy["auto_reply"] is False
    assert dry_store.get_message("om_private") is None
    with dry_store.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks(short_id, status, chat_id, chat_type, created_at, updated_at) "
            "VALUES ('t_dry', 'watching', 'oc_one', 'group', 'now', 'now')"
        )
        dry_task_id = cursor.lastrowid
    assert dry_task_id is not None

    prod_store.delete_chat_product_policy("oc_one", actor="test")
    prod_store.upsert_chat_product_policy(
        {"chat_id": "oc_two", "auto_reply": True}, actor="test"
    )
    _, reopened, _ = _load_runtime(str(path), mode="dry-run")
    assert reopened.get_chat_product_policy("oc_one") is not None
    assert reopened.get_chat_product_policy("oc_two") is None
    assert sync_production_policy(production, reopened)["chat_count"] == 1
    assert reopened.get_chat_product_policy("oc_one") is None
    assert reopened.is_group_policy_held("task", dry_task_id)
    synced_policy = reopened.get_chat_product_policy("oc_two")
    assert synced_policy is not None and synced_policy["auto_reply"] is True
    assert prod_store.get_message("om_private") is not None


def test_dry_run_can_import_yaml_without_creating_production_database(
    tmp_path: Path,
) -> None:
    path = _config(tmp_path)
    assert (
        main(["policy", "import-config", "--config", str(path), "--mode", "dry-run"])
        == 0
    )
    assert not (tmp_path / "data/agent.sqlite3").exists()
    _, store, _ = _load_runtime(str(path), mode="dry-run")
    policy = store.get_chat_product_policy("oc_one")
    assert policy is not None and policy["auto_reply"] is True


def test_dry_run_rejects_incompatible_production_policy_without_modifying_it(
    tmp_path: Path,
) -> None:
    path = _config(tmp_path)
    production = ConfigService().load(path)
    prod_store = SQLiteStore(tmp_path / "data/agent.sqlite3")
    prod_store.import_product_policy_from_config(production.config)
    with prod_store.connect() as conn:
        conn.execute("PRAGMA user_version = 8")
    before = prod_store.path.read_bytes()

    with pytest.raises(ConfigError, match="current schema baseline"):
        _load_runtime(str(path), mode="dry-run")

    assert prod_store.path.read_bytes() == before
    dry_store = SQLiteStore(tmp_path / "data/agent.sqlite3.dry-run")
    assert dry_store.get_product_policy() is None


def test_dry_run_copies_coworker_policy_without_upgrading_v9_source(
    tmp_path: Path,
) -> None:
    path = _config(tmp_path)
    with path.open("a", encoding="utf-8") as output:
        output.write("\ncoworker:\n  enabled: true\n  chat_id: oc_one\n")
    loaded = ConfigService().load(path)
    prod_store = SQLiteStore(tmp_path / "data/agent.sqlite3")
    prod_store.import_product_policy_from_config(loaded.config)
    with prod_store.connect() as conn:
        conn.execute("PRAGMA user_version = 9")
    before = prod_store.path.read_bytes()
    _, dry_store, _ = _load_runtime(str(path), mode="dry-run")
    coworker = dry_store.get_product_policy("ai_coworker")
    assert coworker is not None and coworker["chat_id"] == "oc_one"
    assert prod_store.path.read_bytes() == before


def test_dry_run_rejects_overlapping_resource_roots(tmp_path: Path) -> None:
    path = _config(tmp_path)
    path.write_text(
        path.read_text().replace("resource_dir: data/resources", "resource_dir: .")
    )
    with pytest.raises(ConfigError, match="resource_dir"):
        dry_run_config(ConfigService().load(path))


def test_daemon_dry_run_selects_isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path)
    captured: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_forever(self):
            return 0

    monkeypatch.setattr("feishu_shadow_agent.cli.Daemon", FakeDaemon)
    assert main(["daemon", "--config", str(path), "--dry-run"]) == 0
    store = captured["store"]
    assert isinstance(store, SQLiteStore)
    assert store.path == tmp_path / "data/agent.sqlite3.dry-run"
    assert captured["dry_run"] is True
    assert not (tmp_path / "data/agent.sqlite3").exists()


def test_invalid_production_snapshot_does_not_partially_replace_dry_policy(
    tmp_path: Path,
) -> None:
    path = _config(tmp_path)
    production = ConfigService().load(path)
    prod_store = SQLiteStore(tmp_path / "data/agent.sqlite3")
    prod_store.import_product_policy_from_config(production.config)
    _, dry_store, _ = _load_runtime(str(path), mode="dry-run")
    prod_store.delete_chat_product_policy("oc_one", actor="test")
    prod_store.upsert_chat_product_policy({"chat_id": "oc_bad"}, actor="test")
    with prod_store.connect() as conn:
        conn.execute(
            "UPDATE chat_policies SET reply_identity = 'invalid' WHERE chat_id = 'oc_bad'"
        )

    with pytest.raises(ConfigError, match="snapshot is invalid"):
        sync_production_policy(production, dry_store)

    assert dry_store.get_chat_product_policy("oc_one") is not None
    assert dry_store.get_chat_product_policy("oc_bad") is None
