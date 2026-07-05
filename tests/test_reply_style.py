from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.agent_invocation import AgentInvoker
from feishu_shadow_agent.config import (
    AppConfig,
    OwnerConfig,
    OwnerStyleRefreshConfig,
    ReplyPostprocessConfig,
    ReplyPostprocessOwnerStyleConfig,
)
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.reply_style import ReplyStyleRefresher
from feishu_shadow_agent.types import LarkCliResult, MessagePage


class FakeFeishu:
    def __init__(self, items: list[dict[str, Any]]):
        self.items = items
        self.calls: list[dict[str, Any]] = []

    def version(self) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "--version"], 0)

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "auth"], 0, json_data={})

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
        return MessagePage(self.items)

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

    def task_router(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        raise AssertionError("task_router should not be called")

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        raise AssertionError("task_session should not be called")

    def reply_postprocess(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        raise AssertionError("reply_postprocess should not be called")

    def owner_style_refresh(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        self.prompts.append(prompt)
        self.cwds.append(None if cwd is None else str(cwd))
        output = self.outputs.pop(0)
        if isinstance(output, AgentRunResult):
            return output
        return AgentRunResult(["hermes"], 0, json_data=output)


def _config(*, min_samples: int = 2, max_samples: int = 5) -> AppConfig:
    return AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        reply_postprocess=ReplyPostprocessConfig(
            owner_style=ReplyPostprocessOwnerStyleConfig(
                profile_path="data/owner_style.zh.md",
                refresh=OwnerStyleRefreshConfig(min_samples=min_samples, max_samples=max_samples),
            )
        ),
    )


def _raw(message_id: str, text: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "sender_id": "ou_owner",
        "sender_type": "user",
        "create_time": "2026-07-01T10:00:00+08:00",
        "content": {"text": text},
    }


def _refresher(tmp_path: Path, *, items: list[dict[str, Any]], backend: FakeBackend, config: AppConfig | None = None) -> ReplyStyleRefresher:
    return ReplyStyleRefresher(
        config=config or _config(),
        base_dir=tmp_path,
        feishu_client=FakeFeishu(items),
        agent_backend=backend,
        agent_invoker=AgentInvoker(
            logger=JSONLLogger(tmp_path / "agent.jsonl"),
            retry_delays_seconds=(0.0, 0.0),
        ),
    )


def test_reply_style_refresh_dry_run_filters_samples_without_hermes_or_write(tmp_path: Path) -> None:
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


def test_reply_style_refresh_fails_without_replacing_old_profile_when_samples_are_low(tmp_path: Path) -> None:
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
    backend.outputs.append({"status": "ok", "profile_markdown": "# Owner Reply Style Profile\n\n## Style Summary\n自然。"})
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
