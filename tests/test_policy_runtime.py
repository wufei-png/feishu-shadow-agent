from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    DaemonConfig,
    OwnerConfig,
    ReplyPolicyConfig,
)
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.membership import BotMembershipService
from feishu_shadow_agent.policy import PolicyResolver, ProductPolicyMissingError
from feishu_shadow_agent.processing import ComposedReply, TaskProcessingService
from feishu_shadow_agent.prompt import BaseTaskSessionOutput
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import (
    LarkCliResult,
    NormalizedMessage,
    ResourceRef,
    TaskRecord,
)


class FakeMembershipClient:
    def __init__(self, results: list[LarkCliResult]):
        self.results = results
        self.probes: list[str] = []

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return LarkCliResult(
            ["lark-cli", "auth", "status"],
            0,
            json_data={"identities": {"bot": {"openId": "ou_bot"}}},
        )

    def list_chat_bots(
        self, *, chat_id: str, as_identity: str = "user"
    ) -> LarkCliResult:
        self.probes.append(chat_id)
        return self.results.pop(0)


class FakeAgentBackend:
    provider = "hermes"

    def task_router(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        raise AssertionError("not called")

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        raise AssertionError("not called")


def _config(
    *,
    reply_policy: ReplyPolicyConfig | None = None,
    chats: dict[str, ChatPolicyConfig] | None = None,
) -> AppConfig:
    return AppConfig(
        owner=OwnerConfig(open_id="ou_owner", name="Owner"),
        reply_policy=reply_policy or ReplyPolicyConfig(),
        chats=chats or {},
    )


def _message(
    *,
    chat_id: str,
    chat_type: str = "group",
    direct_mention: bool = True,
    resource: bool = False,
) -> NormalizedMessage:
    resources = []
    if resource:
        resources.append(
            ResourceRef(
                message_id=f"om_{chat_id}",
                file_key=f"img_{chat_id}",
                resource_type="image",
            )
        )
    return NormalizedMessage(
        message_id=f"om_{chat_id}",
        chat_id=chat_id,
        chat_type=chat_type,  # type: ignore[arg-type]
        sender_id="ou_ext",
        sender_name="Ext",
        sender_type="user",
        sender_role="external_user_message",
        sent_at="2026-06-22T10:00:00+08:00",
        thread_id=None,
        reply_to_message_id=None,
        text="hello",
        direct_mention=direct_mention,
        at_all=False,
        resources=resources,
    )


def _task(*, chat_id: str, chat_type: str = "group") -> TaskRecord:
    return TaskRecord(
        id=1,
        short_id="t_1",
        status="watching",
        chat_id=chat_id,
        chat_type=chat_type,
        thread_id=None,
        root_message_id=f"om_{chat_id}",
        task_label=None,
        watch_until=None,
    )


def _reply_decision(
    resolver: PolicyResolver, *, chat_id: str, chat_type: str = "group"
) -> dict[str, Any]:
    decision = resolver.resolve_reply_policy(
        task=_task(chat_id=chat_id, chat_type=chat_type),
        message=_message(chat_id=chat_id, chat_type=chat_type),
        answerability="auto_reply",
        had_forbidden_mentions=False,
        proposed_reply="reply",
        final_reply="reply",
    )
    return {
        "allow": decision.allow,
        "reason": decision.reason,
        "identity": decision.identity,
        "policy_source": decision.policy_source,
    }


def test_policy_resolver_requires_initialized_product_policy(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    resolver = PolicyResolver(store)

    with pytest.raises(ProductPolicyMissingError, match="policy import-config"):
        resolver.resolve_chat_policy("oc_1", "group")


def test_imported_product_policy_drives_effective_policy_cases(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.import_product_policy_from_config(
        _config(
            reply_policy=ReplyPolicyConfig(
                p2p_auto_reply=False, unknown_group_auto_reply=True
            ),
            chats={
                "oc_blocked": ChatPolicyConfig(
                    auto_reply=False, bot_joined=True, resource_download=False
                ),
                "oc_bot": ChatPolicyConfig(
                    auto_reply=True, bot_joined=True, reply_identity="bot_preferred"
                ),
                "oc_fallback": ChatPolicyConfig(
                    auto_reply=True,
                    bot_joined=False,
                    reply_identity="bot_preferred",
                    allow_user_fallback=True,
                ),
                "oc_force_bot": ChatPolicyConfig(
                    auto_reply=True,
                    bot_joined=False,
                    reply_identity="bot",
                    allow_user_fallback=True,
                ),
            },
        )
    )
    resolver = PolicyResolver(store)

    assert _reply_decision(resolver, chat_id="ou_chat", chat_type="p2p") == {
        "allow": False,
        "reason": "p2p_auto_reply_disabled",
        "identity": "user",
        "policy_source": "p2p",
    }
    assert _reply_decision(resolver, chat_id="oc_unknown") == {
        "allow": True,
        "reason": "ok",
        "identity": "user",
        "policy_source": "unknown_group",
    }
    assert _reply_decision(resolver, chat_id="oc_blocked") == {
        "allow": False,
        "reason": "group_auto_reply_disabled",
        "identity": "user",
        "policy_source": "explicit_chat",
    }
    assert _reply_decision(resolver, chat_id="oc_bot") == {
        "allow": True,
        "reason": "ok",
        "identity": "bot",
        "policy_source": "explicit_chat",
    }
    assert _reply_decision(resolver, chat_id="oc_fallback") == {
        "allow": True,
        "reason": "ok",
        "identity": "user",
        "policy_source": "explicit_chat",
    }
    assert _reply_decision(resolver, chat_id="oc_force_bot") == {
        "allow": False,
        "reason": "bot_not_joined",
        "identity": "bot",
        "policy_source": "explicit_chat",
    }

    assert resolver.can_download_resources(
        _message(chat_id="oc_blocked", resource=True)
    ).reason == ("disabled_by_chat_policy")
    assert (
        resolver.can_download_resources(
            _message(chat_id="oc_fallback", resource=True)
        ).reason
        == "bot_not_joined"
    )
    assert (
        resolver.can_download_resources(_message(chat_id="oc_bot", resource=True)).allow
        is True
    )


def test_runtime_membership_absence_dedupes_notification_and_recovers(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        daemon=DaemonConfig(
            bot_membership_ttl_seconds=300, bot_membership_retry_seconds=60
        ),
        chats={
            "oc_runtime": ChatPolicyConfig(
                auto_reply=True,
                bot_joined=True,
                reply_identity="bot_preferred",
                allow_user_fallback=True,
            )
        },
    )
    store.import_product_policy_from_config(config)
    with store.connect() as conn:
        policy_audits_before = conn.execute(
            "SELECT COUNT(*) AS c FROM policy_audits"
        ).fetchone()["c"]
    client = FakeMembershipClient(
        [
            LarkCliResult(["probe"], 0, json_data={"data": {"items": []}}),
            LarkCliResult(
                ["probe"],
                0,
                json_data={"data": {"items": [{"bot_id": "ou_bot"}]}},
            ),
        ]
    )
    now = "2999-06-22T10:00:00+08:00"
    service = BotMembershipService(
        store=store,
        feishu_client=client,
        config=config,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: now,
    )

    first = service.refresh(run_id="run_1")
    cached = service.refresh(run_id="run_2")

    resolver = PolicyResolver(store)
    assert first.absent == 1 and first.notifications == 1
    assert cached.probed == 0
    assert _reply_decision(resolver, chat_id="oc_runtime")["identity"] == "user"
    assert (
        resolver.can_download_resources(
            _message(chat_id="oc_runtime", resource=True)
        ).reason
        == "bot_not_joined"
    )
    now = "2999-06-22T10:05:01+08:00"
    recovered = service.refresh(run_id="run_3")

    assert recovered.present == 1 and recovered.notifications == 1
    assert _reply_decision(resolver, chat_id="oc_runtime")["identity"] == "bot"
    with store.connect() as conn:
        notifications = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE kind = 'owner_notification'"
        ).fetchone()["c"]
        policy_audits_after = conn.execute(
            "SELECT COUNT(*) AS c FROM policy_audits"
        ).fetchone()["c"]
    assert notifications == 2
    assert policy_audits_after == policy_audits_before


def test_unknown_membership_uses_short_retry_without_becoming_absent(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        daemon=DaemonConfig(bot_membership_retry_seconds=60),
        chats={
            "oc_unknown": ChatPolicyConfig(
                auto_reply=True, bot_joined=True, reply_identity="bot"
            )
        },
    )
    store.import_product_policy_from_config(config)
    client = FakeMembershipClient(
        [LarkCliResult(["probe"], 3, error="permission denied")]
    )
    service = BotMembershipService(
        store=store,
        feishu_client=client,
        config=config,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2999-06-22T10:00:00+08:00",
    )

    summary = service.refresh(run_id="run_1")
    policy = PolicyResolver(store).resolve_chat_policy("oc_unknown", "group")

    assert summary.unknown == 1 and summary.notifications == 0
    assert policy.bot_membership_status == "unknown"
    assert policy.bot_joined is True
    fact = store.get_bot_membership_fact("oc_unknown")
    assert fact is not None
    assert fact["next_probe_at"] == "2999-06-22T02:01:00+00:00"


def test_membership_notifications_follow_last_confirmed_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        daemon=DaemonConfig(
            bot_membership_ttl_seconds=1, bot_membership_retry_seconds=1
        ),
        chats={"oc_runtime": ChatPolicyConfig(bot_joined=True)},
    )
    store.import_product_policy_from_config(config)
    client = FakeMembershipClient(
        [
            LarkCliResult(
                ["probe"],
                0,
                json_data={"data": {"items": [{"bot_id": "ou_bot"}]}},
            ),
            LarkCliResult(["probe"], 0, json_data={"data": {"items": []}}),
            LarkCliResult(["probe"], 3, error="permission denied"),
            LarkCliResult(["probe"], 0, json_data={"data": {"items": []}}),
            LarkCliResult(
                ["probe"],
                0,
                json_data={"data": {"items": [{"bot_id": "ou_bot"}]}},
            ),
        ]
    )
    now = "2999-06-22T10:00:00+08:00"
    service = BotMembershipService(
        store=store,
        feishu_client=client,
        config=config,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: now,
    )

    initial = service.refresh(run_id="run_1")
    now = "2999-06-22T10:00:02+08:00"
    absent = service.refresh(run_id="run_2")
    now = "2999-06-22T10:00:04+08:00"
    unknown = service.refresh(run_id="run_3")
    now = "2999-06-22T10:00:06+08:00"
    repeated_absent = service.refresh(run_id="run_4")
    now = "2999-06-22T10:00:08+08:00"
    recovered = service.refresh(run_id="run_5")

    assert initial.notifications == 0
    assert absent.notifications == 1
    assert unknown.notifications == 0
    assert repeated_absent.notifications == 0
    assert recovered.notifications == 1
    with store.connect() as conn:
        notifications = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE kind = 'owner_notification'"
        ).fetchone()["c"]
    assert notifications == 2


def test_runtime_services_ignore_yaml_policy_after_db_import(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    imported = _config(reply_policy=ReplyPolicyConfig(p2p_auto_reply=False))
    store.import_product_policy_from_config(imported)
    runtime_config = _config(reply_policy=ReplyPolicyConfig(p2p_auto_reply=True))
    service = TaskProcessingService(
        store=store,
        config=runtime_config,
        agent_backend=FakeAgentBackend(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
    )

    gate = service._reply_gate(
        task=_task(chat_id="ou_chat", chat_type="p2p"),
        message=_message(chat_id="ou_chat", chat_type="p2p"),
        output=BaseTaskSessionOutput(
            answerability="auto_reply",
            decision_reason=None,
            proposed_reply="reply",
            reply_target_message_id="om_ou_chat",
            watch_action="keep_watching",
        ),
        composed=ComposedReply(text="reply", had_forbidden_mentions=False),
    )

    assert gate == {
        "allow": False,
        "reason": "p2p_auto_reply_disabled",
        "identity": "user",
        "policy_source": "p2p",
    }
