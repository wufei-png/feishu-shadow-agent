from __future__ import annotations

from pathlib import Path

import pytest

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.agent_invocation import AgentInvoker
from feishu_shadow_agent.config import AppConfig, OwnerConfig
from feishu_shadow_agent.context_access import ContextAccessBuilder
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.resource_preflight import resource_preflight_state
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import (
    NormalizedMessage,
    ResourceRef,
    TaskCandidate,
    TaskRecord,
)


def _config() -> AppConfig:
    return AppConfig(owner=OwnerConfig(open_id="ou_owner", name="Owner"))


def _message(
    *,
    message_id: str = "om_1",
    text: str = "hello",
    sent_at: str = "2026-06-22T10:00:00+08:00",
    resources: list[ResourceRef] | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        chat_id="oc_1",
        chat_type="group",
        sender_id="ou_ext",
        sender_name="Ext",
        sender_type="user",
        sender_role="external_user_message",
        sent_at=sent_at,
        thread_id=None,
        reply_to_message_id=None,
        text=text,
        direct_mention=True,
        at_all=False,
        resources=resources or [],
    )


def _task(*, task_id: int = 1, short_id: str = "t_abc") -> TaskRecord:
    return TaskRecord(
        id=task_id,
        short_id=short_id,
        status="watching",
        chat_id="oc_1",
        chat_type="group",
        thread_id=None,
        root_message_id="om_root",
        task_label="Existing task",
        watch_until="2026-06-22T12:00:00+08:00",
    )


def test_agent_invoker_retries_transient_result_but_not_terminal_result(
    tmp_path: Path,
) -> None:
    invoker = AgentInvoker(
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        max_attempts=3,
        retry_delays_seconds=(0.0, 0.0),
    )
    transient_results = iter(
        [
            AgentRunResult(["agent"], 0, error="stdout was not valid json"),
            AgentRunResult(["agent"], 0, json_data={"ok": True}),
        ]
    )

    transient = invoker.call_with_retries(
        lambda: next(transient_results),
        run_id="run_1",
        stage="task_session",
        message_id="om_1",
    )
    terminal = invoker.call_with_retries(
        lambda: AgentRunResult(["agent"], 1, stderr="permission denied"),
        run_id="run_1",
        stage="task_session",
        message_id="om_1",
    )

    assert transient.attempt_count == 2
    assert transient.result is not None and transient.result.ok
    assert terminal.attempt_count == 1
    assert (
        terminal.last_error is not None and "permission denied" in terminal.last_error
    )


def test_context_access_builder_preserves_router_and_task_scope_cards(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()
    builder = ContextAccessBuilder(store=store, config=_config())
    active_task = _task(task_id=1, short_id="t_active")
    historical_task = _task(task_id=2, short_id="t_history")
    message = _message()

    router = builder.router_context_access(
        message=message,
        active_candidates=[TaskCandidate(task=active_task, matched_by="thread")],
        historical=[historical_task],
    )
    task_session = builder.task_session_context_access(
        message=message, task=active_task
    )

    assert router is not None
    assert router["backend"] == "sqlite"
    assert router["read_only_uri"].endswith("agent.sqlite3?mode=ro")
    assert router["allowed_tables"] == [
        "tasks",
        "task_messages",
        "messages",
        "resources",
        "routing_audits",
    ]
    assert router["query_scope"] == {
        "current_message_id": "om_1",
        "active_tasks": [{"id": 1, "short_id": "t_active"}],
        "historical_tasks": [{"id": 2, "short_id": "t_history"}],
    }
    assert task_session is not None
    assert task_session["query_scope"] == {
        "current_message_id": "om_1",
        "task": {"id": 1, "short_id": "t_active"},
    }
    assert task_session["snapshot"] == {
        "type": "bounded_recent_task_messages",
        "message_limit_per_task": 5,
        "tasks": [
            {
                "id": 1,
                "short_id": "t_active",
                "message_count": 0,
                "truncated": False,
                "recent_messages": [],
            }
        ],
    }


def test_context_access_snapshot_includes_bounded_recent_task_messages(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    root = _message(
        message_id="om_01",
        text="root issue",
        sent_at="2026-06-22T10:01:00+08:00",
    )
    store.upsert_message(root)
    task = store.create_task_for_message(
        root,
        watch_until="2026-06-22T12:00:00+08:00",
        task_label="Existing task",
    )
    for index in range(2, 7):
        message = _message(
            message_id=f"om_{index:02d}",
            text=f"follow up {index}",
            sent_at=f"2026-06-22T10:{index:02d}:00+08:00",
        )
        store.upsert_message(message)
        store.attach_message_to_task(
            task.id,
            message,
            watch_until="2026-06-22T12:00:00+08:00",
        )

    builder = ContextAccessBuilder(store=store, config=_config())
    context = builder.task_session_context_access(
        message=_message(message_id="om_06"), task=store.get_task_by_id(task.id)
    )

    assert context is not None
    snapshot_task = context["snapshot"]["tasks"][0]
    assert snapshot_task["message_count"] == 6
    assert snapshot_task["truncated"] is True
    assert [row["message_id"] for row in snapshot_task["recent_messages"]] == [
        "om_02",
        "om_03",
        "om_04",
        "om_05",
        "om_06",
    ]
    assert snapshot_task["recent_messages"][-1]["text"] == "follow up 6"


@pytest.mark.parametrize(
    ("status", "reason", "retryable"),
    [
        ("downloaded", "ok", False),
        ("bot_not_joined", "resource_needs_bot", False),
        ("skipped", "resource_download_disabled", False),
        ("too_large", "resource_too_large", False),
        ("quota_exceeded", "resource_quota_exceeded", False),
        ("failed", "resource_download_failed", True),
    ],
)
def test_resource_preflight_state_preserves_status_mapping(
    status: str,
    reason: str,
    retryable: bool,
) -> None:
    message = _message()
    state = resource_preflight_state(
        [
            {
                "message_id": "om_1",
                "file_key": "img_1",
                "resource_type": "image",
                "download_status": status,
            }
        ],
        message=message,
        prompt_message_ids=["om_1"],
    )

    assert state["allow"] is (status == "downloaded")
    assert state["reason"] == reason
    assert state["retryable"] is retryable


def test_resource_preflight_state_retries_missing_current_resource_record() -> None:
    resource = ResourceRef(message_id="om_1", file_key="img_1", resource_type="image")
    state = resource_preflight_state(
        [], message=_message(resources=[resource]), prompt_message_ids=["om_1"]
    )

    assert state == {
        "allow": False,
        "reason": "resource_missing",
        "retryable": True,
        "error": "missing resource records: image:img_1",
    }
