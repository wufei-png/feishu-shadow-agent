# ruff: noqa: S101
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from gateway.bot_loop_guard import BotLoopGuard, BotLoopGuardSettings
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from plugins.platforms.feishu.adapter import FeishuAdapter

PROJECT = Path(__file__).resolve().parents[3]


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "coworker"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("FEISHU_ALLOW_BOTS", "none")
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps(
            {
                "hermes_gateway_coworker": {
                    "groups": {
                        "oc_one": {
                            "partners": {
                                "Build": {
                                    "open_id": "ou_build",
                                    "description": "builds",
                                },
                                "Build[]": {"open_id": "ou_build_brackets"},
                                "Build|  release": {"open_id": "ou_build_spaces"},
                                "@diff": {"open_id": "ou_build_reference"},
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    instance = FeishuAdapter(
        PlatformConfig(
            extra={
                "shadow_agent_config": str(config),
                "shadow_agent_source": str(PROJECT / "src"),
                "shadow_agent_python": sys.executable,
                "default_group_policy": "open",
            }
        )
    )
    instance._bot_open_id = "ou_me"
    instance._bot_user_id = ""
    instance._bot_name = "Hermes"
    return instance


def _sender(open_id, kind="bot"):
    return SimpleNamespace(
        sender_type=kind,
        sender_id=SimpleNamespace(open_id=open_id, user_id=None, union_id=None),
    )


def _message(chat_id="oc_one", chat_type="group", target="ou_me", text="@_user_1 done"):
    mention = SimpleNamespace(
        key="@_user_1", id=SimpleNamespace(open_id=target, user_id=None), name="Hermes"
    )
    return SimpleNamespace(
        chat_id=chat_id,
        chat_type=chat_type,
        message_type="text",
        message_id="om_one",
        content=json.dumps({"text": text}),
        mentions=[mention] if target else [],
        thread_id="omt_one",
        parent_id=None,
        upper_message_id=None,
        root_id=None,
    )


def test_filters_bot_senders_before_batching_and_preserves_human_admission(adapter):
    assert adapter._admit(_sender("ou_build"), _message()) is None
    assert adapter._admit(_sender("ou_other"), _message()) is not None
    assert adapter._admit(_sender("ou_build"), _message(chat_id="oc_other")) is not None
    assert adapter._admit(_sender("ou_build"), _message(chat_type="p2p")) is not None
    assert (
        adapter._admit(_sender("ou_build"), _message(target=None, text="@_all done"))
        is not None
    )
    assert adapter._admit(_sender("ou_me"), _message()) is not None
    assert adapter._admit(_sender("ou_human", "user"), _message()) is None


@pytest.mark.asyncio
async def test_real_adapter_appends_prompt_and_marks_only_partner_turns(adapter):
    adapter._extract_message_content = AsyncMock(
        return_value=("/status", MessageType.COMMAND, [], [], [], [])
    )
    adapter._fetch_message_text = AsyncMock(return_value=None)
    adapter.get_chat_info = AsyncMock(return_value={"name": "Test", "type": "group"})
    adapter._resolve_sender_profile = AsyncMock(
        return_value={"user_id": "u_build", "user_name": "Build", "user_id_alt": None}
    )
    adapter._dispatch_inbound_event = AsyncMock()
    adapter.config.extra["channel_prompts"] = {"oc_one": "existing"}

    message = _message()
    await adapter._process_inbound_message(
        data=None,
        message=message,
        sender_id=_sender("ou_build").sender_id,
        chat_type="group",
        message_id="om_one",
        is_bot=True,
    )
    event = adapter._dispatch_inbound_event.await_args.args[0]
    assert event.message_type == MessageType.TEXT
    assert not event.allow_gateway_control
    assert event.source.user_id == "ou_build"
    assert event.get_command() is None
    assert event.channel_prompt.startswith("existing\n\n群聊伙伴：")
    assert "Build" in event.channel_prompt and "builds" in event.channel_prompt
    assert "--profile coworker" in event.channel_prompt

    other = _message(chat_id="oc_other")
    await adapter._process_inbound_message(
        data=None,
        message=other,
        sender_id=_sender("ou_human", "user").sender_id,
        chat_type="group",
        message_id="om_two",
        is_bot=False,
    )
    event2 = adapter._dispatch_inbound_event.await_args.args[0]
    assert event2.channel_prompt is None
    assert event2.allow_gateway_control
    assert not adapter._text_batch_is_compatible(event, event2)

    private = _message(chat_type="p2p")
    await adapter._process_inbound_message(
        data=None,
        message=private,
        sender_id=_sender("ou_human", "user").sender_id,
        chat_type="p2p",
        message_id="om_three",
        is_bot=False,
    )
    assert (
        adapter._dispatch_inbound_event.await_args.args[0].channel_prompt == "existing"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("feishu_name", [None, "Different Feishu name"])
@pytest.mark.parametrize(
    ("open_id", "alias"),
    [
        ("ou_build", "Build"),
        ("ou_build_brackets", "Build[]"),
        ("ou_build_spaces", "Build|  release"),
        ("ou_build_reference", "@diff"),
    ],
)
async def test_partner_turn_uses_configured_identity_when_feishu_name_differs(
    adapter, feishu_name, open_id, alias
):
    adapter._extract_message_content = AsyncMock(
        return_value=("done", MessageType.TEXT, [], [], [], [])
    )
    adapter._fetch_message_text = AsyncMock(return_value=None)
    adapter.get_chat_info = AsyncMock(return_value={"name": "Test", "type": "group"})
    adapter._resolve_sender_profile = AsyncMock(
        return_value={
            "user_id": "u_build",
            "user_name": feishu_name,
            "user_id_alt": None,
        }
    )
    adapter._dispatch_inbound_event = AsyncMock()

    await adapter._process_inbound_message(
        data=None,
        message=_message(),
        sender_id=_sender(open_id).sender_id,
        chat_type="group",
        message_id="om_one",
        is_bot=True,
    )

    event = adapter._dispatch_inbound_event.await_args.args[0]
    assert event.source.user_id == open_id
    assert event.source.user_name == feishu_name
    assert event.text == "done"
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        group_sessions_per_user=False, thread_sessions_per_user=False
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._consume_pending_native_image_paths = Mock()
    runner._expand_inbound_context_references = AsyncMock(return_value="expanded")
    partner_input = await runner._prepare_inbound_message_text(
        event=event, source=event.source, history=[], session_key="key"
    )
    assert partner_input.startswith(
        f"[伙伴消息：{json.dumps(alias, ensure_ascii=False)}]\n"
    )
    runner._expand_inbound_context_references.assert_not_awaited()

    reference = MessageEvent(
        text="please check @diff", source=event.source, allow_gateway_control=False
    )
    reference_input = await runner._prepare_inbound_message_text(
        event=reference, source=reference.source, history=[], session_key="key"
    )
    assert (
        reference_input
        == f"[伙伴消息：{json.dumps(alias, ensure_ascii=False)}]\nexpanded"
    )
    runner._expand_inbound_context_references.assert_awaited_once()
    assert (
        "伙伴消息" not in runner._expand_inbound_context_references.await_args.args[-1]
    )

    internal = MessageEvent(
        text="plugin notification",
        source=event.source,
        internal=True,
        allow_gateway_control=False,
    )
    internal_input = await runner._prepare_inbound_message_text(
        event=internal, source=internal.source, history=[], session_key="key"
    )
    assert not internal_input.startswith("[伙伴消息：")
    adapter._resolve_sender_profile = AsyncMock(
        return_value={
            "user_id": "u_human",
            "user_name": f"伙伴：{alias}",
            "user_id_alt": None,
        }
    )
    await adapter._process_inbound_message(
        data=None,
        message=_message(),
        sender_id=_sender("ou_human", "user").sender_id,
        chat_type="group",
        message_id="om_human",
        is_bot=False,
    )
    human = adapter._dispatch_inbound_event.await_args.args[0]
    human_input = await runner._prepare_inbound_message_text(
        event=human, source=human.source, history=[], session_key="key"
    )
    assert human.source.user_name == f"伙伴：{alias}"
    assert not human_input.startswith("[伙伴消息：")
    assert human_input != partner_input


def test_partner_chains_are_scoped_by_topic_but_rate_limit_stays_per_group(adapter):
    guard = BotLoopGuard(settings=BotLoopGuardSettings)
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda _source: adapter
    runner._adapter_profile_for_source = lambda _source: "coworker"
    runner._under_authorization_profile = lambda _source, check: check()
    runner._bot_loop_guard_instance = lambda: guard

    def event(
        thread_id,
        *,
        chat_id="oc_one",
        user_id="ou_build",
        is_bot=True,
        text="done",
        platform=Platform.FEISHU,
    ):
        source = SessionSource(
            platform=platform,
            chat_id=chat_id,
            chat_type="group",
            thread_id=thread_id,
            user_id=user_id,
            is_bot=is_bot,
        )
        return MessageEvent(source=source, text=text)

    topic_a, topic_b = event("omt_a"), event("omt_b")
    assert runner._admit_bot_chain(topic_a)
    assert runner._admit_bot_chain(topic_b)
    assert not runner._admit_bot_chain(topic_a)
    runner._note_bot_reply(topic_a.source)
    assert not runner._admit_bot_chain(event("omt_a", text="another result"))
    assert runner._admit_bot_chain(event("omt_b", text="another result"))
    assert runner._admit_bot_chain(event("omt_a", user_id="ou_human", is_bot=False))
    assert runner._admit_bot_chain(topic_a)
    assert not runner._admit_bot_chain(topic_b)
    for index in range(4):
        assert runner._admit_bot_chain(event("omt_c", text=f"result {index}"))
    assert not runner._admit_bot_chain(event("omt_c", text="result 4"))

    # Groups without a partner roster retain the native group-scoped chain.
    assert runner._admit_bot_chain(event("omt_a", chat_id="oc_other"))
    assert not runner._admit_bot_chain(event("omt_b", chat_id="oc_other"))
    assert runner._admit_bot_chain(event("thread_a", platform=Platform.TELEGRAM))
    assert not runner._admit_bot_chain(event("thread_b", platform=Platform.TELEGRAM))

    for index in range(20):
        assert runner._admit_bot_message((topic_a if index % 2 else topic_b).source)
    assert not runner._admit_bot_message(topic_a.source)


def test_partner_authorization_does_not_require_enabling_all_bots(adapter):
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda _source: adapter
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_one",
        chat_type="group",
        user_id="ou_build",
        is_bot=True,
    )
    with patch("gateway.authz_mixin._auth_env", return_value=""):
        assert runner._chat_scoped_grant(source, None, True, False)
        source.user_id = "ou_other"
        assert not runner._chat_scoped_grant(source, None, True, False)
        source.user_id = "ou_build"
        source.chat_id = "oc_other"
        assert not runner._chat_scoped_grant(source, None, True, False)


def test_partner_metadata_survives_native_queue():
    from gateway.session_envelope import _snapshot_native, restore_native

    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_one",
        chat_type="group",
        user_id="ou_build",
        is_bot=True,
    )
    event = MessageEvent(text="/status", source=source, allow_gateway_control=False)
    runner = SimpleNamespace(
        session_store=SimpleNamespace(_generate_session_key=lambda _: "key")
    )
    encoded_source = source.to_dict()
    encoded_source["is_bot"] = True
    payload = _snapshot_native(runner, event, None, encoded_source=encoded_source)
    restored = restore_native(payload)
    assert restored.source.is_bot
    assert restored.source.user_id == "ou_build"
    assert not restored.allow_gateway_control
    assert restored.get_command() is None


@pytest.mark.asyncio
async def test_partner_busy_messages_queue_in_both_gateway_paths():
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_one",
        chat_type="group",
        user_id="ou_build",
        is_bot=True,
    )
    event = MessageEvent(text="yes", source=source, allow_gateway_control=False)
    runner = object.__new__(GatewayRunner)
    runner._queue_or_replace_pending_event = Mock()
    runner._is_user_authorized_for_source = Mock(return_value=True)
    runner._admit_bot_message_for_source = Mock(return_value=True)
    runner._admit_bot_chain = Mock(return_value=True)
    runner._effective_busy_input_mode = Mock(return_value="interrupt")
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._hm_post_admission_consume = AsyncMock(return_value=(False, None))
    runner._draining = False

    assert await runner._hm_busy_preempt(event, source, "key") is False
    assert await runner._hm_handle_running_session_message(event, source, "key") is None
    assert await runner._handle_active_session_busy_message(event, "key") is True
    assert runner._queue_or_replace_pending_event.call_count == 2
    runner._route_plaintext_approval_while_busy.assert_not_awaited()
    runner._admit_bot_message_for_source.assert_called_once()
    runner._admit_bot_chain.assert_called_once()

    with (
        patch("gateway.run_inbound.pending_setup_busy_notice", return_value=None),
        patch(
            "gateway.session_ingress.admit_message",
            new_callable=AsyncMock,
            return_value="queued",
        ) as admit,
    ):
        assert await runner._hm_admit_to_fifo(object(), event, source, "key", True) == (
            True,
            "queued",
        )
        admit.assert_awaited_once()


def test_incomplete_integration_fails_during_adapter_startup(tmp_path):
    with pytest.raises(ValueError):
        FeishuAdapter(
            PlatformConfig(extra={"shadow_agent_config": str(tmp_path / "config.yaml")})
        )
