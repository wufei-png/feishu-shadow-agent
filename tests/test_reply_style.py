from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.agent_invocation import AgentInvoker
from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    OwnerConfig,
    OwnerStyleRefreshConfig,
    ReplyPostprocessConfig,
    ReplyPostprocessOwnerStyleConfig,
)
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.reply_style import ReplyStyleRefresher
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import LarkCliResult, MessagePage


class FakeFeishu:
    def __init__(self, items: list[dict[str, Any]]):
        self.items = items
        self.calls: list[dict[str, Any]] = []

    def version(self) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "--version"], 0)

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return LarkCliResult(
            ["lark-cli", "auth"],
            0,
            json_data={"identities": {"bot": {"openId": "ou_bot"}}},
        )

    def list_user_p2p_chats(self, *, page_token: str | None = None) -> MessagePage:
        return MessagePage(
            [
                {
                    "chat_id": row["chat_id"],
                    "chat_mode": "p2p",
                    "p2p_target_id": row.get("peer_id", "ou_person"),
                    "p2p_target_type": "user",
                }
                for row in self.items
                if row.get("chat_type") == "p2p"
            ]
        )

    def owner_message(self, **kwargs: Any) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "send"], 0)

    def reply_message(self, **kwargs: Any) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "reply"], 0)

    def get_messages(self, **kwargs: Any) -> MessagePage:
        return MessagePage([])

    def search_messages(self, **kwargs: Any) -> MessagePage:
        return MessagePage([])

    def search_owner_messages(self, **kwargs: Any) -> MessagePage:
        self.calls.append(kwargs)
        return MessagePage(
            [
                row
                for row in self.items
                if row.get("chat_type") == kwargs.get("chat_type")
                and (
                    not kwargs.get("chat_id") or row.get("chat_id") == kwargs["chat_id"]
                )
            ]
        )

    def list_chat_messages(self, **kwargs: Any) -> MessagePage:
        return MessagePage([])

    def list_p2p_messages(self, **kwargs: Any) -> MessagePage:
        return MessagePage([])

    def list_thread_messages(self, **kwargs: Any) -> MessagePage:
        return MessagePage([])

    def download_resource(self, **kwargs: Any) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "download"], 0)


class FakeBackend:
    provider = "hermes"

    def __init__(self):
        self.outputs: list[dict[str, Any] | AgentRunResult] = []
        self.prompts: list[str] = []
        self.cwds: list[str | None] = []

    def task_router(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        raise AssertionError("task_router should not be called")

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        raise AssertionError("task_session should not be called")

    def reply_postprocess(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        raise AssertionError("reply_postprocess should not be called")

    def owner_style_refresh(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        self.prompts.append(prompt)
        self.cwds.append(None if cwd is None else str(cwd))
        output = self.outputs.pop(0)
        if isinstance(output, AgentRunResult):
            return output
        return AgentRunResult(["hermes"], 0, json_data=output)


def _config(*, min_samples: int = 2, max_samples: int = 5) -> AppConfig:
    return AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        chats={"oc_allowed": ChatPolicyConfig()},
        reply_postprocess=ReplyPostprocessConfig(
            owner_style=ReplyPostprocessOwnerStyleConfig(
                profile_path="data/owner_style.zh.md",
                refresh=OwnerStyleRefreshConfig(
                    min_samples=min_samples, max_samples=max_samples
                ),
            )
        ),
    )


def _raw(message_id: str, text: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat_id": "oc_allowed",
        "chat_type": "group",
        "sender_id": "ou_owner",
        "sender_type": "user",
        "create_time": "2026-07-01T10:00:00+08:00",
        "content": {"text": text},
    }


def _refresher(
    tmp_path: Path,
    *,
    items: list[dict[str, Any]],
    backend: FakeBackend,
    config: AppConfig | None = None,
) -> ReplyStyleRefresher:
    effective_config = config or _config()
    store = SQLiteStore(tmp_path / "style.sqlite3")
    store.import_product_policy_from_config(effective_config)
    return ReplyStyleRefresher(
        config=effective_config,
        store=store,
        base_dir=tmp_path,
        feishu_client=FakeFeishu(items),
        agent_backend=backend,
        agent_invoker=AgentInvoker(
            logger=JSONLLogger(tmp_path / "agent.jsonl"),
            retry_delays_seconds=(0.0, 0.0),
        ),
    )


def test_group_sampling_checks_policy_before_each_page(tmp_path: Path) -> None:
    backend = FakeBackend()
    refresher = _refresher(tmp_path, items=[], backend=backend)

    class RemovingClient(FakeFeishu):
        def search_owner_messages(self, **kwargs: Any) -> MessagePage:
            self.calls.append(kwargs)
            if kwargs["chat_type"] == "group":
                assert kwargs["page_all"] is False
                refresher.store.delete_chat_product_policy("oc_allowed", actor="owner")
                return MessagePage([_raw("om_1", "hello")], "next", True)
            return MessagePage([])

    client = RemovingClient([])
    refresher.feishu_client = client
    result = refresher.refresh(dry_run=False, run_id="run")

    assert result.status == "failed"
    assert len([call for call in client.calls if call["chat_type"] == "group"]) == 1
    assert backend.prompts == []


def test_group_sampling_follows_pages_while_policy_is_current(tmp_path: Path) -> None:
    refresher = _refresher(tmp_path, items=[], backend=FakeBackend())

    class PaginatedClient(FakeFeishu):
        def search_owner_messages(self, **kwargs: Any) -> MessagePage:
            self.calls.append(kwargs)
            if kwargs["chat_type"] != "group":
                return MessagePage([])
            if kwargs["page_token"] is None:
                return MessagePage([_raw("om_1", "hello")], "next", True)
            return MessagePage([_raw("om_2", "world")])

    client = PaginatedClient([])
    refresher.feishu_client = client
    result = refresher.refresh(dry_run=True, run_id="run")

    assert result.pulled_count == 2
    assert [call["page_token"] for call in client.calls] == [None, "next"]


def test_reply_style_refresh_dry_run_filters_samples_without_hermes_or_write(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    items = [
        _raw("om_1", "可以，晚点我看下"),
        _raw("om_2", "/approve a_1"),
        _raw("om_3", "https://example.com/a"),
        _raw("om_4", "[image]"),
        _raw("om_5", "x" * 1001),
        _raw("om_6", "先按这个方向推进"),
    ]
    refresher = _refresher(tmp_path, items=items, backend=backend)

    result = refresher.refresh(dry_run=True, run_id="run_1")

    assert result.status == "dry_run"
    assert result.pulled_count == 6
    assert result.filtered_count == 2
    assert result.selected_count == 2
    assert result.hermes_called is False
    assert backend.prompts == []
    assert not (tmp_path / "data" / "owner_style.zh.md").exists()


def test_reply_style_preview_scopes_groups_and_excludes_bot_p2p(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    allowed = _raw("om_allowed", "allowed")
    foreign = _raw("om_foreign", "foreign") | {"chat_id": "oc_foreign"}
    direct = _raw("om_p2p", "direct") | {"chat_id": "oc_p2p", "chat_type": "p2p"}
    bot = _raw("om_bot", "bot") | {
        "chat_id": "oc_bot",
        "chat_type": "p2p",
        "peer_id": "ou_bot",
    }
    refresher = _refresher(
        tmp_path, items=[allowed, foreign, direct, bot], backend=backend
    )

    result = refresher.refresh(dry_run=True, run_id="run_1")

    client = refresher.feishu_client
    assert isinstance(client, FakeFeishu)
    assert [(call["chat_type"], call["chat_id"]) for call in client.calls] == [
        ("group", "oc_allowed"),
        ("p2p", "oc_p2p"),
    ]
    assert result.pulled_count == result.selected_count == 2


def test_reply_style_preview_skips_p2p_if_enumeration_fails(tmp_path: Path) -> None:
    refresher = _refresher(
        tmp_path, items=[_raw("om_allowed", "allowed")], backend=FakeBackend()
    )

    class FailedEnumeration(FakeFeishu):
        def list_user_p2p_chats(self, *, page_token: str | None = None) -> MessagePage:
            raise RuntimeError("incomplete enumeration")

    client = FailedEnumeration([_raw("om_allowed", "allowed")])
    refresher.feishu_client = client
    result = refresher.refresh(dry_run=True, run_id="run_1")

    assert result.selected_count == 1
    assert result.stats["p2p_scope_error"] == "incomplete enumeration"
    assert [call["chat_type"] for call in client.calls] == ["group"]


def test_reply_style_refresh_fails_without_replacing_old_profile_when_samples_are_low(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "data" / "owner_style.zh.md"
    profile.parent.mkdir()
    profile.write_text("old profile\n", encoding="utf-8")
    backend = FakeBackend()
    refresher = _refresher(tmp_path, items=[_raw("om_1", "可以")], backend=backend)

    result = refresher.refresh(dry_run=False, run_id="run_1")

    assert result.status == "failed"
    assert result.hermes_called is False
    assert profile.read_text(encoding="utf-8") == "old profile\n"


def test_reply_style_refresh_success_writes_profile(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.outputs.append(
        {
            "status": "ok",
            "profile_markdown": "# Owner Reply Style Profile\n\n## Style Summary\n自然。",
        }
    )
    refresher = _refresher(
        tmp_path,
        items=[_raw("om_1", "可以，晚点我看下"), _raw("om_2", "先按这个方向推进")],
        backend=backend,
    )

    result = refresher.refresh(dry_run=False, run_id="run_1")

    profile = tmp_path / "data" / "owner_style.zh.md"
    assert result.status == "written"
    assert result.hermes_called is True
    assert result.wrote_profile is True
    assert profile.read_text(encoding="utf-8").startswith("# Owner Reply Style Profile")
    assert backend.prompts


def test_reply_style_refresh_failed_hermes_leaves_old_profile(tmp_path: Path) -> None:
    profile = tmp_path / "data" / "owner_style.zh.md"
    profile.parent.mkdir()
    profile.write_text("old profile\n", encoding="utf-8")
    backend = FakeBackend()
    backend.outputs.append(AgentRunResult(["hermes"], 1, error="boom"))
    refresher = _refresher(
        tmp_path,
        items=[_raw("om_1", "可以，晚点我看下"), _raw("om_2", "先按这个方向推进")],
        backend=backend,
    )

    result = refresher.refresh(dry_run=False, run_id="run_1")

    assert result.status == "failed"
    assert result.hermes_called is True
    assert profile.read_text(encoding="utf-8") == "old profile\n"


@pytest.mark.parametrize(
    ("profile_markdown", "error"),
    [
        ("# Owner Reply Style Profile\n\nSee https://example.com/private\n", "URL"),
        ("# Owner Reply Style Profile\n\nmessage om_secret1234\n", "Feishu identifier"),
        ("# Owner Reply Style Profile\n\ncall 13812345678\n", "phone number"),
    ],
)
def test_reply_style_refresh_rejects_profile_with_private_artifacts(
    tmp_path: Path,
    profile_markdown: str,
    error: str,
) -> None:
    profile = tmp_path / "data" / "owner_style.zh.md"
    profile.parent.mkdir()
    profile.write_text("old profile\n", encoding="utf-8")
    backend = FakeBackend()
    backend.outputs.append({"status": "ok", "profile_markdown": profile_markdown})
    refresher = _refresher(
        tmp_path,
        items=[_raw("om_1", "可以，晚点我看下"), _raw("om_2", "先按这个方向推进")],
        backend=backend,
    )

    result = refresher.refresh(dry_run=False, run_id="run_1")

    assert result.status == "failed"
    assert result.hermes_called is True
    assert error in (result.error or "")
    assert profile.read_text(encoding="utf-8") == "old profile\n"
