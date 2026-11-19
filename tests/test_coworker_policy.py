from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.agent_backend_factory import create_coworker_backend
from feishu_shadow_agent.codex import CodexCliClient
from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    CoworkerConfig,
    CoworkerPartnerConfig,
    LoadedConfig,
    OwnerConfig,
)
from feishu_shadow_agent.health import HealthSuite
from feishu_shadow_agent.store.policy_store import COWORKER_POLICY_KEY
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult


def test_coworker_policy_is_imported_and_replaced_explicitly(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="owner"),
        coworker=CoworkerConfig(enabled=True, chat_id="chat_a"),
    )

    store.import_product_policy_from_config(config)
    assert store.get_product_policy(COWORKER_POLICY_KEY)["chat_id"] == "chat_a"  # type: ignore[index]
    config.coworker.chat_id = "chat_b"
    store.import_product_policy_from_config(config)
    assert store.get_product_policy(COWORKER_POLICY_KEY)["chat_id"] == "chat_a"  # type: ignore[index]
    store.import_product_policy_from_config(config, replace=True)
    assert store.get_product_policy(COWORKER_POLICY_KEY)["chat_id"] == "chat_b"  # type: ignore[index]
    config.coworker.enabled = False
    store.import_product_policy_from_config(config, replace=True)
    assert store.get_product_policy(COWORKER_POLICY_KEY)["enabled"] is False  # type: ignore[index]


def test_coworker_requires_a_single_explicit_chat() -> None:
    with pytest.raises(ValidationError, match="chat_id is required"):
        CoworkerConfig(enabled=True)


def test_new_memory_default_preserves_existing_sqlite_policy(tmp_path: Path) -> None:
    assert CoworkerConfig().memory_maintenance is False
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="owner"),
        coworker=CoworkerConfig(enabled=True, chat_id="group"),
    )
    store.import_product_policy_from_config(config)
    with store.connect() as conn:
        conn.execute(
            "UPDATE product_policies SET policy_json = json_remove(policy_json, '$.memory_maintenance') WHERE key = ?",
            (COWORKER_POLICY_KEY,),
        )
    assert store.get_product_policy(COWORKER_POLICY_KEY)["memory_maintenance"] is True  # type: ignore[index]
    updated = store.update_coworker_policy(
        {"history_source": "local_cache"}, actor="test"
    )
    assert updated["policy"]["memory_maintenance"] is True


def test_daily_schedule_accepts_legacy_interval_but_does_not_reuse_it() -> None:
    legacy = CoworkerConfig.model_validate(
        {"memory_maintenance_interval_seconds": 21600}
    )
    assert legacy.memory_maintenance_time == "00:00"
    assert "memory_maintenance_interval_seconds" not in legacy.model_dump()
    configured = CoworkerConfig.model_validate(
        {
            "memory_maintenance_interval_seconds": 3600,
            "memory_maintenance_time": "04:30",
        }
    )
    assert configured.memory_maintenance_time == "04:30"
    with pytest.raises(ValidationError, match="HH:MM"):
        CoworkerConfig(memory_maintenance_time="24:00")


def test_partner_descriptions_persist_and_legacy_ids_remain_valid(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="owner"),
        coworker=CoworkerConfig(
            enabled=True,
            chat_id="group",
            partner_bots={
                "Legacy": "old-id",
                "Partner": CoworkerPartnerConfig(
                    open_id="new-id", description="检查结果并反馈"
                ),
            },
        ),
    )
    store.import_product_policy_from_config(config)
    raw = store.get_product_policy(COWORKER_POLICY_KEY)
    assert raw is not None
    restored = CoworkerConfig.model_validate(raw)
    assert restored.partner_open_id("Legacy") == "old-id"
    assert restored.partner_open_id("Partner") == "new-id"
    assert restored.partner_bots["Partner"] == CoworkerPartnerConfig(
        open_id="new-id", description="检查结果并反馈"
    )


def test_partner_ids_must_be_unique_across_legacy_and_detailed_entries() -> None:
    with pytest.raises(ValidationError, match="open IDs must be unique"):
        CoworkerConfig(
            partner_bots={
                "Legacy": "same-id",
                "Partner": CoworkerPartnerConfig(open_id="same-id"),
            }
        )


def test_coworker_backend_inherits_native_settings_without_changing_shadow_config(
    tmp_path: Path,
) -> None:
    config = AppConfig(owner=OwnerConfig(open_id="owner"))
    policy = CoworkerConfig(enabled=True, chat_id="chat_a", agent_timeout_seconds=60)
    backend = create_coworker_backend(config, policy, base_dir=tmp_path)

    assert isinstance(backend, CodexCliClient)
    assert backend.config_scope == "native"
    assert backend.auto_context == "enabled"
    assert backend.config.timeout_seconds == 60
    assert backend.inherit_native_instructions
    assert config.agent_backend.provider == "hermes"
    assert config.tool_permissions == "read_only"


def test_doctor_checks_coworker_backend_and_requires_matching_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(
        owner=OwnerConfig(open_id="owner"),
        coworker=CoworkerConfig(enabled=True, chat_id="group"),
        chats={"group": ChatPolicyConfig(auto_reply=True)},
    )
    db = SQLiteStore(tmp_path / "agent.sqlite3")
    db.import_product_policy_from_config(config)
    loaded = LoadedConfig(
        config=config, path=tmp_path / "config.yaml", base_dir=tmp_path, raw={}
    )
    observed: list[str] = []

    def checker(value: LoadedConfig) -> HealthCheckResult:
        observed.append(value.config.agent_backend.provider)
        return HealthCheckResult("backend", "critical", "ok", "ok")

    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "example-secret")
    monkeypatch.setattr(
        "feishu_shadow_agent.health.importlib.util.find_spec", lambda _name: object()
    )
    monkeypatch.setattr(
        "feishu_shadow_agent.health._run_codex_command",
        lambda argv, **_kwargs: AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout="multi_agent stable true\n"
            if "features" in argv
            else "app-server help",
        ),
    )
    suite = HealthSuite(
        loaded_config=loaded,
        store=db,
        feishu_client=cast(Any, object()),
        agent_backend_checker=checker,
    )
    results = suite._check_coworker({"appId": "app"})
    assert results[0].status == "ok" and results[1].name == "coworker_backend"
    assert {item.name: item.status for item in results}[
        "coworker_codex_multi_agent"
    ] == "ok"
    assert observed == ["codex"]
    assert suite._check_coworker({"appId": "different"})[0].status == "failed"
