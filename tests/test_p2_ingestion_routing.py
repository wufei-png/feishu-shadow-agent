from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    DaemonConfig,
    LifecycleConfig,
    OwnerConfig,
    ReplyPolicyConfig,
)
from feishu_shadow_agent.daemon import Daemon
from feishu_shadow_agent.ingestion import IngestionService, MessageNormalizer
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.routing import MessageRouter
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult, LarkCliResult, MessagePage


class FakeHealthSuite:
    run_id = "run_p2"

    def run(self, *, send_test: bool = False) -> list[HealthCheckResult]:
        return [HealthCheckResult("config_schema", "critical", "ok", "ok")]


class FakeFeishuClient:
    def __init__(self):
        self.search_pages: dict[tuple[str, bool, str | None], MessagePage | Exception] = {}
        self.chat_pages: dict[tuple[str, str | None], MessagePage | Exception] = {}
        self.thread_pages: dict[tuple[str, str | None], MessagePage | Exception] = {}
        self.downloads: list[dict[str, str]] = []
        self.calls: list[str] = []
        self.write_download_files = True

    def version(self) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "--version"], 0, stdout="lark-cli version 1.0.56")

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "auth"], 0, json_data={})

    def owner_message(
        self,
        *,
        owner_open_id: str,
        text: str,
        idempotency_key: str,
        dry_run: bool = True,
    ) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "im", "+messages-send"], 0, json_data={})

    def search_messages(
        self,
        *,
        chat_type: str,
        is_at_me: bool,
        start: str | None,
        end: str | None,
        page_token: str | None = None,
        query: str = "",
        page_size: int = 50,
    ) -> MessagePage:
        self.calls.append(f"search:{chat_type}:{is_at_me}:{page_token}")
        value = self.search_pages.get((chat_type, is_at_me, page_token), MessagePage([]))
        if isinstance(value, Exception):
            raise value
        return value

    def list_chat_messages(
        self,
        *,
        chat_id: str,
        start: str | None,
        end: str | None,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> MessagePage:
        self.calls.append(f"chat:{chat_id}:{page_token}")
        value = self.chat_pages.get((chat_id, page_token), MessagePage([]))
        if isinstance(value, Exception):
            raise value
        return value

    def list_thread_messages(
        self,
        *,
        thread_id: str,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> MessagePage:
        self.calls.append(f"thread:{thread_id}:{page_token}")
        value = self.thread_pages.get((thread_id, page_token), MessagePage([]))
        if isinstance(value, Exception):
            raise value
        return value

    def download_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        output: str,
    ) -> LarkCliResult:
        self.downloads.append(
            {
                "message_id": message_id,
                "file_key": file_key,
                "resource_type": resource_type,
                "output": output,
            }
        )
        if self.write_download_files:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"resource")
        return LarkCliResult(["lark-cli", "im", "+messages-resources-download"], 0, json_data={})


class FailingOnceRouter:
    def __init__(self, store: SQLiteStore):
        self.inner = MessageRouter(store=store)
        self.failed = False

    def route(self, *args: Any, **kwargs: Any):
        if not self.failed:
            self.failed = True
            raise RuntimeError("route failed after message insert")
        return self.inner.route(*args, **kwargs)


class FailingAuditStore(SQLiteStore):
    def __init__(self, path: Path):
        super().__init__(path)
        self.fail_next_audit = True

    def _record_routing_audit(self, *args: Any, **kwargs: Any) -> None:
        if self.fail_next_audit:
            self.fail_next_audit = False
            raise RuntimeError("audit failed after route mutation")
        return super()._record_routing_audit(*args, **kwargs)


class FailingResourceStore(SQLiteStore):
    def __init__(self, path: Path):
        super().__init__(path)
        self.fail_next_resource = True

    def upsert_resource(self, *args: Any, **kwargs: Any) -> None:
        if self.fail_next_resource:
            self.fail_next_resource = False
            raise RuntimeError("resource store failed")
        return super().upsert_resource(*args, **kwargs)


def _config(**kwargs: Any) -> AppConfig:
    return AppConfig(owner=OwnerConfig(open_id="ou_owner", name="Owner"), **kwargs)


def _message(
    message_id: str,
    *,
    chat_id: str = "oc_1",
    chat_type: str = "group",
    sender_id: str = "ou_ext",
    sender_type: str = "user",
    create_time: str = "2026-06-22T10:00:00+08:00",
    text: str = "hello",
    mentions: list[dict[str, str]] | None = None,
    thread_id: str | None = None,
    reply_to: str | None = None,
    image_key: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {"text": text}
    if mentions is not None:
        content["mentions"] = mentions
    if image_key:
        content["image_key"] = image_key
    raw: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_type": sender_type,
        "create_time": create_time,
        "content": content,
    }
    if thread_id:
        raw["thread_id"] = thread_id
    if reply_to:
        raw["reply_to_message_id"] = reply_to
    return raw


def test_normalizer_marks_mentions_sender_roles_and_resources() -> None:
    normalizer = MessageNormalizer(owner_open_id="ou_owner")

    direct = normalizer.normalize(
        _message("om_1", mentions=[{"open_id": "ou_owner"}], image_key="img_1"),
        default_chat_type="group",
    )
    at_all = normalizer.normalize(
        _message("om_2", text="@所有人 hello", mentions=[{"id": "all"}]),
        default_chat_type="group",
    )
    html_direct = normalizer.normalize(
        {
            "message_id": "om_html_direct",
            "chat_id": "oc_1",
            "chat_type": "group",
            "sender": {"id": "ou_ext", "sender_type": "user"},
            "create_time": "2026-06-22 10:00",
            "content": '<at user_id="ou_owner">Owner</at> hello',
        },
        default_chat_type="group",
    )
    html_at_all = normalizer.normalize(
        {
            "message_id": "om_html_all",
            "chat_id": "oc_1",
            "chat_type": "group",
            "sender": {"id": "ou_ext", "sender_type": "user"},
            "create_time": "2026-06-22 10:00",
            "content": '<at user_id="all"></at> hello',
        },
        default_chat_type="group",
    )
    rendered_at_all = normalizer.normalize(
        _message("om_rendered_all", text="@_all hello"),
        default_chat_type="group",
    )
    owner = normalizer.normalize(
        _message("om_3", sender_id="ou_owner"),
        default_chat_type="group",
    )
    bot = normalizer.normalize(
        _message("om_4", sender_id="cli_bot", sender_type="bot"),
        default_chat_type="group",
    )
    app_bot = normalizer.normalize(
        _message("om_5", sender_id="cli_app", sender_type="app"),
        default_chat_type="group",
    )
    markdown_resources = normalizer.normalize(
        _message("om_6", text="![Image](img_v3_0212t_abc-123)\n[file](file_v2_xyz-456)"),
        default_chat_type="group",
    )

    assert direct.direct_mention is True
    assert direct.resources[0].file_key == "img_1"
    assert at_all.at_all is True
    assert at_all.direct_mention is False
    assert html_direct.direct_mention is True
    assert html_direct.mentions == ["ou_owner"]
    assert html_at_all.at_all is True
    assert html_at_all.direct_mention is False
    assert rendered_at_all.at_all is True
    assert rendered_at_all.direct_mention is False
    assert owner.sender_role == "owner_message"
    assert bot.sender_role == "bot_message"
    assert app_bot.sender_role == "bot_message"
    assert {(resource.resource_type, resource.file_key) for resource in markdown_resources.resources} == {
        ("image", "img_v3_0212t_abc-123"),
        ("file", "file_v2_xyz-456"),
    }


def test_group_ingest_drains_pages_sorts_dedupes_and_advances_checkpoint(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    fake = FakeFeishuClient()
    fake.search_pages[("group", True, None)] = MessagePage(
        [
            _message("om_2", create_time="2026-06-22T10:02:00+08:00", mentions=[{"open_id": "ou_owner"}]),
            _message("om_1", create_time="2026-06-22T10:01:00+08:00", mentions=[{"open_id": "ou_owner"}]),
        ],
        next_page_token="p2",
        has_more=True,
    )
    fake.search_pages[("group", True, "p2")] = MessagePage(
        [
            _message("om_1", create_time="2026-06-22T10:01:00+08:00", mentions=[{"open_id": "ou_owner"}]),
            _message("om_3", create_time="2026-06-22T10:03:00+08:00", mentions=[{"open_id": "ou_owner"}]),
        ]
    )
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(daemon=DaemonConfig(overlap_seconds=120)),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    result = service.ingest_group_at_me(run_id="run_1")

    assert result.processed == 4
    assert store.get_checkpoint("ingest.group_at_me") == {"last_success_at": "2026-06-22T10:10:00+08:00"}
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == 3
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 1
        routes = [row["route"] for row in conn.execute("SELECT route FROM routing_audits ORDER BY id")]
    assert routes == ["new_task", "ignore", "ambiguous", "ambiguous"]


def test_failed_pagination_does_not_advance_checkpoint(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    fake = FakeFeishuClient()
    fake.search_pages[("p2p", False, None)] = MessagePage(
        [_message("om_1", chat_type="p2p")],
        next_page_token="p2",
        has_more=True,
    )
    fake.search_pages[("p2p", False, "p2")] = RuntimeError("boom")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    try:
        service.ingest_p2p(run_id="run_1")
    except RuntimeError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("pagination failure did not raise")

    assert store.get_checkpoint("ingest.p2p") is None


def test_existing_message_without_route_audit_is_routed_on_retry(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    router = FailingOnceRouter(store)
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        router=router,  # type: ignore[arg-type]
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    raw = _message("om_retry", chat_type="p2p", sender_id="ou_a")

    try:
        service.process_raw_message(raw, source="p2p", default_chat_type="p2p", run_id="run_1")
    except RuntimeError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("router failure did not raise")

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM routing_audits").fetchone()["c"] == 0

    retried = service.process_raw_message(raw, source="p2p", default_chat_type="p2p", run_id="run_1")

    assert retried is not None
    assert retried.decision.route == "new_task"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 1
        audit = conn.execute(
            "SELECT route FROM routing_audits WHERE message_id = ?",
            ("om_retry",),
        ).fetchone()
    assert audit["route"] == "new_task"


def test_route_audit_failure_rolls_back_task_mutation(tmp_path: Path) -> None:
    store = FailingAuditStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    raw = _message("om_audit_retry", chat_type="p2p", sender_id="ou_a")

    with pytest.raises(RuntimeError, match="audit failed"):
        service.process_raw_message(raw, source="p2p", default_chat_type="p2p", run_id="run_1")

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM routing_audits").fetchone()["c"] == 0

    retried = service.process_raw_message(raw, source="p2p", default_chat_type="p2p", run_id="run_1")

    assert retried is not None
    assert retried.decision.route == "new_task"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 1
        audit = conn.execute(
            "SELECT route FROM routing_audits WHERE message_id = ?",
            ("om_audit_retry",),
        ).fetchone()
    assert audit["route"] == "new_task"


def test_resource_store_failure_is_retried_for_duplicate_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = FailingResourceStore(tmp_path / "agent.sqlite3")
    fake = FakeFeishuClient()
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(chats={"oc_1": ChatPolicyConfig(bot_joined=True)}),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    raw = _message("om_resource_retry", mentions=[{"open_id": "ou_owner"}], image_key="img_retry")

    with pytest.raises(RuntimeError, match="resource store failed"):
        service.process_raw_message(raw, source="group_at_me", default_chat_type="group", run_id="run_1")

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM routing_audits").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM resources").fetchone()["c"] == 0
    assert len(fake.downloads) == 1

    retried = service.process_raw_message(
        raw,
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    assert retried is not None
    assert retried.decision.reason == "duplicate_message"
    assert len(fake.downloads) == 2
    with store.connect() as conn:
        resource = conn.execute(
            "SELECT download_status FROM resources WHERE file_key = ?",
            ("img_retry",),
        ).fetchone()
    assert resource["download_status"] == "downloaded"


def test_p2p_single_active_sender_candidate_uses_router_placeholder(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    first = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    second = service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert first is not None and first.decision.route == "new_task"
    assert second is not None and second.decision.route == "ambiguous"
    assert second.decision.reason == "router_placeholder"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM task_messages").fetchone()["c"] == 1


def test_p2p_single_active_without_candidate_creates_new_task(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    first = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    second = service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_b"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert first is not None and first.decision.route == "new_task"
    assert second is not None and second.decision.route == "new_task"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 2


def test_top_level_reply_to_string_uses_reply_shortcut(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    reply = _message("om_reply", sender_id="ou_other")
    reply["reply_to"] = "om_root"

    attached = service.process_raw_message(
        reply,
        source="active_watch",
        default_chat_type="group",
        run_id="run_1",
    )

    assert attached is not None
    assert attached.decision.route == "attach_task"
    assert attached.decision.matched_by == "reply_to_msg"
    with store.connect() as conn:
        row = conn.execute(
            "SELECT reply_to_message_id FROM messages WHERE message_id = ?",
            ("om_reply",),
        ).fetchone()
    assert row["reply_to_message_id"] == "om_root"


def test_thread_unique_match_uses_thread_shortcut(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", thread_id="omt_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None

    attached = service.process_raw_message(
        _message("om_thread_follow", thread_id="omt_1", sender_id="ou_other"),
        source="active_watch",
        default_chat_type="group",
        run_id="run_1",
    )

    assert attached is not None
    assert attached.decision.route == "attach_task"
    assert attached.decision.matched_by == "thread"


def test_owner_takeover_closes_task_and_cancels_pending_work(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    store.insert_action_for_test(idempotency_key="reply_1", task_id=created.task.id)
    store.insert_approval_for_test(short_id="a_1", task_id=created.task.id)

    takeover = service.process_raw_message(
        _message("om_owner", chat_id="ou_chat", chat_type="p2p", sender_id="ou_owner", reply_to="om_1"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert takeover is not None and takeover.decision.route == "human_taken_over"
    with store.connect() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        action = conn.execute("SELECT status FROM actions WHERE idempotency_key = ?", ("reply_1",)).fetchone()
        approval = conn.execute("SELECT status FROM approvals WHERE short_id = ?", ("a_1",)).fetchone()
    assert task["status"] == "human_taken_over"
    assert action["status"] == "cancelled"
    assert approval["status"] == "expired"


def test_owner_takeover_cancels_pending_approval_notification(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_id = store.create_send_reply_approval(
        task_id=created.task.id,
        preview="manual reply",
        payload={"reply_target_message_id": "om_1", "text": "manual reply"},
        notify_payload={"reason": "needs_owner_approval"},
    )
    with store.connect() as conn:
        notification = conn.execute(
            "SELECT id, status, approval_id FROM actions WHERE kind = 'owner_notification'"
        ).fetchone()
    assert notification["status"] == "pending"
    assert notification["approval_id"] == approval_id

    takeover = service.process_raw_message(
        _message("om_owner", chat_id="ou_chat", chat_type="p2p", sender_id="ou_owner", reply_to="om_1"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert takeover is not None and takeover.decision.route == "human_taken_over"
    dispatchable_ids = {action.id for action in store.list_dispatchable_actions()}
    with store.connect() as conn:
        approval = conn.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        notification_after = conn.execute(
            "SELECT status FROM actions WHERE id = ?",
            (notification["id"],),
        ).fetchone()
    assert approval["status"] == "expired"
    assert notification_after["status"] == "cancelled"
    assert notification["id"] not in dispatchable_ids


def test_owner_takeover_expires_tool_action_pending_approval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    store.insert_approval_for_test(short_id="a_tool", task_id=created.task.id, kind="tool_action")

    takeover = service.process_raw_message(
        _message("om_owner", chat_id="ou_chat", chat_type="p2p", sender_id="ou_owner", reply_to="om_1"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert takeover is not None and takeover.decision.route == "human_taken_over"
    with store.connect() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        approval = conn.execute("SELECT status FROM approvals WHERE short_id = ?", ("a_tool",)).fetchone()
    assert task["status"] == "human_taken_over"
    assert approval["status"] == "expired"


def test_p2p_owner_message_without_structural_match_does_not_take_over(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    store.insert_action_for_test(idempotency_key="reply_1", task_id=created.task.id)
    store.insert_approval_for_test(short_id="a_1", task_id=created.task.id)

    result = service.process_raw_message(
        _message("om_owner", chat_id="ou_chat", chat_type="p2p", sender_id="ou_owner"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert result is not None
    assert result.decision.route == "ignore"
    assert result.decision.reason == "owner_message_not_task_intervention"
    with store.connect() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        action = conn.execute("SELECT status FROM actions WHERE idempotency_key = ?", ("reply_1",)).fetchone()
        approval = conn.execute("SELECT status FROM approvals WHERE short_id = ?", ("a_1",)).fetchone()
    assert task["status"] == "watching"
    assert action["status"] == "pending"
    assert approval["status"] == "pending"


def test_owner_takeover_accepts_top_level_reply_to_string(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    owner_reply = _message("om_owner_reply", sender_id="ou_owner")
    owner_reply["reply_to"] = "om_root"

    takeover = service.process_raw_message(
        owner_reply,
        source="active_watch",
        default_chat_type="group",
        run_id="run_1",
    )

    assert takeover is not None
    assert takeover.decision.route == "human_taken_over"
    with store.connect() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
    assert task["status"] == "human_taken_over"


def test_owner_takeover_matches_thread_watch_key_without_task_thread_id(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    follow_up = service.process_raw_message(
        _message("om_follow", sender_id="ou_a", reply_to="om_root", thread_id="omt_1"),
        source="active_watch",
        default_chat_type="group",
        run_id="run_1",
    )
    assert follow_up is not None and follow_up.decision.route == "attach_task"

    takeover = service.process_raw_message(
        _message("om_owner_thread", sender_id="ou_owner", thread_id="omt_1"),
        source="active_watch",
        default_chat_type="group",
        run_id="run_1",
    )

    assert takeover is not None
    assert takeover.decision.route == "human_taken_over"
    with store.connect() as conn:
        task = conn.execute(
            "SELECT status, thread_id FROM tasks WHERE id = ?",
            (created.task.id,),
        ).fetchone()
    assert task["status"] == "human_taken_over"
    assert task["thread_id"] is None


def test_sent_user_identity_agent_message_is_self_message(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    store.insert_action_for_test(
        idempotency_key="reply_sent",
        task_id=created.task.id,
        status="sent",
        result={"sent_message_id": "om_agent_reply"},
    )

    agent_reply = service.process_raw_message(
        _message(
            "om_agent_reply",
            sender_id="ou_owner",
            reply_to="om_root",
            text="agent reply sent as user",
        ),
        source="active_watch",
        default_chat_type="group",
        run_id="run_1",
    )

    assert agent_reply is not None
    assert agent_reply.decision.route == "ignore"
    assert agent_reply.decision.reason == "self_message"
    with store.connect() as conn:
        task = conn.execute(
            "SELECT status, last_agent_reply, watch_until FROM tasks WHERE id = ?",
            (created.task.id,),
        ).fetchone()
        task_message = conn.execute(
            "SELECT role FROM task_messages WHERE task_id = ? AND message_id = ?",
            (created.task.id, "om_agent_reply"),
        ).fetchone()
    assert task["status"] == "watching"
    assert task["last_agent_reply"] == "agent reply sent as user"
    assert task["watch_until"] == "2026-06-22T12:10:00+08:00"
    assert task_message["role"] == "agent_reply"


def test_closed_recall_records_router_placeholder(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    store.close_task_for_owner_takeover(created.task.id)

    recalled = service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert recalled is not None
    assert recalled.decision.route == "ambiguous"
    assert recalled.decision.reason == "closed_recall_router_placeholder"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 1


def test_lifecycle_watch_minutes_controls_new_task_watch_until(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(lifecycle=LifecycleConfig(watch_minutes=5)),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert created is not None and created.task is not None
    with store.connect() as conn:
        task = conn.execute("SELECT watch_until FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
    assert task["watch_until"] == "2026-06-22T10:15:00+08:00"


def test_lifecycle_closed_recall_days_bounds_historical_candidates(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(lifecycle=LifecycleConfig(closed_recall_days=1)),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    store.close_task_for_owner_takeover(created.task.id)
    with store.connect() as conn:
        conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            ("2026-06-20T10:10:00+08:00", created.task.id),
        )

    routed = service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert routed is not None
    assert routed.decision.route == "new_task"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 2


def test_unrelated_closed_task_does_not_block_new_group_task(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    old_task = service.process_raw_message(
        _message(
            "om_old",
            chat_id="oc_1",
            sender_id="ou_a",
            text="分类服务启动失败",
            mentions=[{"open_id": "ou_owner"}],
        ),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert old_task is not None and old_task.task is not None
    store.close_task_for_owner_takeover(old_task.task.id)

    new_task = service.process_raw_message(
        _message(
            "om_new",
            chat_id="oc_1",
            sender_id="ou_b",
            text="新的报表权限问题",
            mentions=[{"open_id": "ou_owner"}],
        ),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    assert new_task is not None
    assert new_task.decision.route == "new_task"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 2


def test_reply_to_closed_task_enters_recall_placeholder(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    old_task = service.process_raw_message(
        _message(
            "om_old",
            chat_id="oc_1",
            sender_id="ou_a",
            text="分类服务启动失败",
            mentions=[{"open_id": "ou_owner"}],
        ),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert old_task is not None and old_task.task is not None
    store.close_task_for_owner_takeover(old_task.task.id)

    recalled = service.process_raw_message(
        _message(
            "om_reply",
            chat_id="oc_1",
            sender_id="ou_b",
            text="我这边也是这个问题",
            mentions=[{"open_id": "ou_owner"}],
            reply_to="om_old",
        ),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    assert recalled is not None
    assert recalled.decision.route == "ambiguous"
    assert recalled.decision.reason == "closed_recall_router_placeholder"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 1


def test_resource_status_downloaded_and_bot_not_joined(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeFeishuClient()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(chats={"oc_1": ChatPolicyConfig(bot_joined=True)}),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    service.process_raw_message(
        _message("om_img", mentions=[{"open_id": "ou_owner"}], image_key="img_1"),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    service_no_bot = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(chats={"oc_2": ChatPolicyConfig(bot_joined=False)}),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    service_no_bot.process_raw_message(
        _message("om_img_2", chat_id="oc_2", mentions=[{"open_id": "ou_owner"}], image_key="img_2"),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    assert fake.downloads[0]["file_key"] == "img_1"
    assert fake.downloads[0]["output"].startswith("data/resources/om_img/image_")
    assert not Path(fake.downloads[0]["output"]).is_absolute()
    with store.connect() as conn:
        statuses = {
            row["file_key"]: row["download_status"]
            for row in conn.execute("SELECT file_key, download_status FROM resources")
        }
        stored_path = conn.execute(
            "SELECT path FROM resources WHERE file_key = ?",
            ("img_1",),
        ).fetchone()["path"]
    assert statuses == {"img_1": "downloaded", "img_2": "bot_not_joined"}
    assert stored_path.startswith("data/resources/om_img/image_")
    assert not Path(stored_path).is_absolute()


def test_unknown_group_resource_download_is_independent_from_auto_reply(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    service.process_raw_message(
        _message("om_unknown_img", chat_id="oc_unknown", mentions=[{"open_id": "ou_owner"}], image_key="img_unknown"),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        resource = conn.execute(
            "SELECT download_status, raw_json FROM resources WHERE file_key = ?",
            ("img_unknown",),
        ).fetchone()
    assert resource["download_status"] == "bot_not_joined"
    assert "unknown_group" in resource["raw_json"]


def test_resource_download_disabled_skips_even_when_auto_reply_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeFeishuClient()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(
            reply_policy=ReplyPolicyConfig(unknown_group_auto_reply=True),
            chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True, resource_download=False)},
        ),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    service.process_raw_message(
        _message("om_img_skip", mentions=[{"open_id": "ou_owner"}], image_key="img_skip"),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        resource = conn.execute(
            "SELECT download_status, raw_json FROM resources WHERE file_key = ?",
            ("img_skip",),
        ).fetchone()
    assert fake.downloads == []
    assert resource["download_status"] == "skipped"
    assert "disabled_by_chat_policy" in resource["raw_json"]


def test_resource_download_success_without_file_records_missing_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeFeishuClient()
    fake.write_download_files = False
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(chats={"oc_1": ChatPolicyConfig(bot_joined=True)}),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    service.process_raw_message(
        _message("om_missing_file", mentions=[{"open_id": "ou_owner"}], image_key="img_missing"),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        resource = conn.execute(
            "SELECT download_status, path FROM resources WHERE file_key = ?",
            ("img_missing",),
        ).fetchone()
    assert resource["download_status"] == "missing_file"
    assert Path(resource["path"]).parent.exists()


def test_active_watch_ignores_unmatched_resource_message_without_download(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeFeishuClient()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(chats={"oc_1": ChatPolicyConfig(bot_joined=True)}),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    service.process_raw_message(
        _message("om_root", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    ignored = service.process_raw_message(
        _message("om_unmatched", sender_id="ou_other", image_key="img_unmatched"),
        source="active_watch",
        default_chat_type="group",
        run_id="run_1",
    )

    assert ignored is not None and ignored.decision.route == "ignore"
    assert fake.downloads == []
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM resources").fetchone()["c"] == 0


def test_active_watch_suppresses_real_rendered_at_all_from_watched_sender(tmp_path: Path) -> None:
    fake = FakeFeishuClient()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", sender_id="ou_a", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None

    at_all = service.process_raw_message(
        _message("om_all", sender_id="ou_a", text="@_all please check"),
        source="active_watch",
        default_chat_type="group",
        run_id="run_1",
    )

    assert at_all is not None
    assert at_all.decision.route == "ignore"
    assert at_all.decision.reason == "at_all_suppressed"


def test_group_active_watch_only_processes_watch_key_followups(tmp_path: Path) -> None:
    fake = FakeFeishuClient()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(daemon=DaemonConfig(overlap_seconds=120)),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message(
            "om_root",
            chat_id="oc_1",
            sender_id="ou_a",
            mentions=[{"open_id": "ou_owner"}],
        ),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    direct_mention_new_task = _message(
        "om_new_at",
        chat_id="oc_1",
        sender_id="ou_b",
        text="新的权限问题",
        mentions=[{"open_id": "ou_owner"}],
        create_time="2026-06-22T10:06:00+08:00",
    )
    follow_up = _message(
        "om_follow",
        chat_id="oc_1",
        sender_id="ou_a",
        text="还是同一个问题",
        create_time="2026-06-22T10:07:00+08:00",
    )
    fake.chat_pages[("oc_1", None)] = MessagePage([direct_mention_new_task, follow_up])

    result = service.run_active_watch(run_id="run_1")

    assert result.processed == 1
    assert store.get_message("om_follow") is not None
    assert store.get_message("om_new_at") is None


def test_group_active_watch_leaves_new_direct_mention_for_group_ingest(tmp_path: Path) -> None:
    fake = FakeFeishuClient()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(daemon=DaemonConfig(overlap_seconds=120)),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message(
            "om_root",
            chat_id="oc_1",
            sender_id="ou_a",
            mentions=[{"open_id": "ou_owner"}],
        ),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    direct_mention_new_task = _message(
        "om_new_at",
        chat_id="oc_1",
        sender_id="ou_b",
        text="新的权限问题",
        mentions=[{"open_id": "ou_owner"}],
    )
    fake.chat_pages[("oc_1", None)] = MessagePage([direct_mention_new_task])

    active_watch_result = service.run_active_watch(run_id="run_1")
    fake.search_pages[("group", True, None)] = MessagePage([direct_mention_new_task])
    group_result = service.ingest_group_at_me(run_id="run_1")

    assert active_watch_result.processed == 0
    assert group_result.processed == 1
    assert store.get_message("om_new_at") is not None
    with store.connect() as conn:
        audit = conn.execute(
            "SELECT route, route_reason FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_new_at",),
        ).fetchone()
    assert audit["route"] == "ambiguous"
    assert audit["route_reason"] == "router_placeholder"


def test_thread_active_watch_filters_by_checkpoint_window(tmp_path: Path) -> None:
    fake = FakeFeishuClient()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(daemon=DaemonConfig(overlap_seconds=120)),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:20:00+08:00",
    )
    created = service.process_raw_message(
        _message(
            "om_thread_root",
            thread_id="omt_1",
            mentions=[{"open_id": "ou_owner"}],
            create_time="2026-06-22T10:00:00+08:00",
        ),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    store.set_checkpoint("active_watch.thread.omt_1", {"last_success_at": "2026-06-22T10:10:00+08:00"})
    fake.thread_pages[("omt_1", None)] = MessagePage(
        [
            _message(
                "om_old_thread",
                thread_id="omt_1",
                create_time="2026-06-22T10:07:59+08:00",
            ),
            _message(
                "om_new_thread",
                thread_id="omt_1",
                create_time="2026-06-22T10:11:00+08:00",
            ),
        ]
    )

    result = service.run_active_watch(run_id="run_1")

    assert result.processed == 1
    assert store.get_checkpoint("active_watch.thread.omt_1") == {
        "last_success_at": "2026-06-22T10:20:00+08:00"
    }
    assert store.get_message("om_old_thread") is None
    assert store.get_message("om_new_thread") is not None


def test_thread_active_watch_target_comes_from_watch_key_without_task_thread_id(tmp_path: Path) -> None:
    fake = FakeFeishuClient()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=_config(daemon=DaemonConfig(overlap_seconds=120)),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:20:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    attached = service.process_raw_message(
        _message("om_follow", reply_to="om_root", thread_id="omt_1"),
        source="active_watch",
        default_chat_type="group",
        run_id="run_1",
    )
    assert attached is not None and attached.decision.route == "attach_task"
    fake.thread_pages[("omt_1", None)] = MessagePage(
        [
            _message(
                "om_thread_new",
                thread_id="omt_1",
                create_time="2026-06-22T10:19:00+08:00",
            ),
        ]
    )

    result = service.run_active_watch(run_id="run_1")

    assert result.processed == 1
    assert fake.calls == ["thread:omt_1:None"]
    assert store.get_message("om_thread_new") is not None
    with store.connect() as conn:
        task = conn.execute(
            "SELECT thread_id FROM tasks WHERE id = ?",
            (created.task.id,),
        ).fetchone()
    assert task["thread_id"] is None


def test_daemon_tick_runs_p2_stages_in_order(tmp_path: Path) -> None:
    fake = FakeFeishuClient()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    daemon = Daemon(
        store=store,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        health_suite=FakeHealthSuite(),  # type: ignore[arg-type]
        tick_interval_seconds=1,
        dry_run=True,
        app_config=_config(),
        feishu_client=fake,
    )

    results = daemon.run_one_tick(run_id="run_1")

    assert [result.name for result in results] == [
        "approval_inbox",
        "group_at_me",
        "p2p",
        "active_watch",
        "dispatch",
        "retention",
    ]
    assert fake.calls == ["search:group:True:None", "search:p2p:False:None"]


def test_approval_inbox_placeholder_does_not_write_checkpoint(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishuClient(),
        config=_config(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )

    result = service.run_approval_inbox_placeholder(run_id="run_1")

    assert result.name == "approval_inbox"
    assert result.ok is True
    assert store.get_checkpoint("approval_inbox") is None
