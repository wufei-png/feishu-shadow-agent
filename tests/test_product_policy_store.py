from __future__ import annotations

from pathlib import Path

from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    OwnerConfig,
    ReplyPolicyConfig,
)
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _config(
    *,
    reply_policy: ReplyPolicyConfig | None = None,
    chats: dict[str, ChatPolicyConfig] | None = None,
) -> AppConfig:
    return AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        reply_policy=reply_policy or ReplyPolicyConfig(),
        chats=chats or {},
    )


def test_product_policy_probe_reports_missing_then_initialized(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    assert store.product_policy_initialization_probe() == {
        "initialized": False,
        "missing": ["global:reply_policy"],
    }

    result = store.import_product_policy_from_config(_config(), used_defaults=True)

    assert result["used_defaults"] is True
    assert result["inserted"]["global"] == ["reply_policy"]
    assert result["audit_count"] == 1
    assert store.product_policy_initialization_probe() == {
        "initialized": True,
        "missing": [],
    }
    assert store.get_product_policy() == {
        "reply_policy": {
            "p2p_auto_reply": True,
            "unknown_group_auto_reply": False,
        },
        "default_chat_policy": {
            "bot_joined": False,
            "reply_identity": "bot_preferred",
            "allow_user_fallback": True,
            "resource_download": True,
        },
    }


def test_default_import_skips_existing_policies_and_keeps_db_only_chats(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    initial = _config(
        chats={
            "oc_keep": ChatPolicyConfig(name="Keep", auto_reply=True, bot_joined=True),
            "oc_db_only": ChatPolicyConfig(name="DB only", auto_reply=True),
        },
    )
    store.import_product_policy_from_config(initial)

    next_config = _config(
        reply_policy=ReplyPolicyConfig(
            p2p_auto_reply=False, unknown_group_auto_reply=True
        ),
        chats={
            "oc_keep": ChatPolicyConfig(name="Changed", auto_reply=False),
            "oc_new": ChatPolicyConfig(name="New", auto_reply=True),
        },
    )
    result = store.import_product_policy_from_config(next_config)

    assert result["skipped"]["global"] == ["reply_policy"]
    assert result["skipped"]["chats"] == ["oc_keep"]
    assert result["inserted"]["chats"] == ["oc_new"]
    assert result["audit_count"] == 1
    product_policy = store.get_product_policy()
    keep_policy = store.get_chat_product_policy("oc_keep")
    db_only_policy = store.get_chat_product_policy("oc_db_only")
    assert product_policy is not None
    assert keep_policy is not None
    assert db_only_policy is not None
    assert product_policy["reply_policy"] == {
        "p2p_auto_reply": True,
        "unknown_group_auto_reply": False,
    }
    assert keep_policy["name"] == "Keep"
    assert keep_policy["auto_reply"] is True
    assert db_only_policy["name"] == "DB only"


def test_replace_updates_global_and_listed_chats_without_deleting_absent_chats(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    initial = _config(
        chats={
            "oc_replace": ChatPolicyConfig(
                name="Before", auto_reply=True, bot_joined=True
            ),
            "oc_absent": ChatPolicyConfig(name="Absent from import", auto_reply=True),
        },
    )
    store.import_product_policy_from_config(initial)

    replacement = _config(
        reply_policy=ReplyPolicyConfig(
            p2p_auto_reply=False, unknown_group_auto_reply=True
        ),
        chats={
            "oc_replace": ChatPolicyConfig(
                name="After",
                auto_reply=False,
                bot_joined=False,
                reply_identity="user",
                allow_user_fallback=False,
                resource_download=False,
            )
        },
    )
    result = store.import_product_policy_from_config(replacement, replace=True)

    assert result["replaced"]["global"] == ["reply_policy"]
    assert result["replaced"]["chats"] == ["oc_replace"]
    assert result["audit_count"] == 2
    product_policy = store.get_product_policy()
    absent_policy = store.get_chat_product_policy("oc_absent")
    assert product_policy is not None
    assert absent_policy is not None
    assert product_policy["reply_policy"] == {
        "p2p_auto_reply": False,
        "unknown_group_auto_reply": True,
    }
    assert store.get_chat_product_policy("oc_replace") == {
        "chat_id": "oc_replace",
        "name": "After",
        "auto_reply": False,
        "bot_joined": False,
        "reply_identity": "user",
        "allow_user_fallback": False,
        "resource_download": False,
    }
    assert absent_policy["name"] == "Absent from import"
    chat_audit = next(
        audit
        for audit in store.list_policy_audits(limit=10)
        if audit["policy_key"] == "chat:oc_replace"
    )
    assert chat_audit["actor"] == "import_config"
    assert chat_audit["old_json"]["auto_reply"] is True
    assert chat_audit["new_json"]["auto_reply"] is False


def test_direct_policy_updates_persist_actor_reason_and_audit(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.import_product_policy_from_config(
        _config(
            reply_policy=ReplyPolicyConfig(
                p2p_auto_reply=False, unknown_group_auto_reply=False
            ),
            chats={"oc_direct": ChatPolicyConfig(name="Direct", auto_reply=True)},
        )
    )

    global_result = store.update_product_policy(
        {
            "reply_policy": {
                "p2p_auto_reply": False,
                "unknown_group_auto_reply": True,
            },
            "default_chat_policy": {
                "bot_joined": False,
                "reply_identity": "bot_preferred",
                "allow_user_fallback": True,
                "resource_download": True,
            },
        },
        actor="test_operator",
        reason="global policy edit",
    )
    chat_result = store.upsert_chat_product_policy(
        {
            "chat_id": "oc_direct",
            "name": "Direct",
            "auto_reply": False,
            "bot_joined": False,
            "reply_identity": "bot_preferred",
            "allow_user_fallback": True,
            "resource_download": True,
        },
        actor="test_operator",
        reason="chat policy edit",
    )

    assert global_result["changed"] is True
    assert chat_result["changed"] is True
    assert (
        store.get_product_policy()["reply_policy"]["unknown_group_auto_reply"] is True
    )
    assert store.get_chat_product_policy("oc_direct")["auto_reply"] is False
    audits = store.list_policy_audits(limit=2)
    assert [audit["actor"] for audit in audits] == ["test_operator", "test_operator"]
    assert [audit["reason"] for audit in audits] == [
        "chat policy edit",
        "global policy edit",
    ]
    assert audits[0]["old_json"]["auto_reply"] is True
    assert audits[0]["new_json"]["auto_reply"] is False
