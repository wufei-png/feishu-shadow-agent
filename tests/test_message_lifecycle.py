from __future__ import annotations

from pathlib import Path
from typing import Any

from feishu_shadow_agent.config import AppConfig, OwnerConfig, ReplyPolicyConfig
from feishu_shadow_agent.ingestion import MessageNormalizer
from feishu_shadow_agent.operator_queries.message_detail import MessageDetailQuery
from feishu_shadow_agent.routing import CandidateCollector, MessageRouter
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import NormalizedMessage


def _config() -> AppConfig:
    return AppConfig(
        owner=OwnerConfig(open_id="ou_owner", name="Owner"),
        reply_policy=ReplyPolicyConfig(),
        chats={},
    )


def _raw(
    message_id: str,
    *,
    chat_id: str = "oc_1",
    chat_type: str = "group",
    sender_id: str = "ou_ext",
    create_time: str = "2026-06-22T10:00:00+08:00",
    text: str = "hello",
    msg_type: str | None = None,
    content: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text} if content is None else content
    raw: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_type": "user",
        "create_time": create_time,
        "content": body,
        **extra,
    }
    if msg_type is not None:
        raw["msg_type"] = msg_type
    return raw


def _normalize(raw: dict[str, Any]) -> NormalizedMessage:
    return MessageNormalizer(owner_open_id="ou_owner").normalize(
        raw, default_chat_type=raw.get("chat_type")
    )


def test_normalizer_records_message_type_variants() -> None:
    assert _normalize(_raw("om_1", msg_type="text")).message_type == "text"
    assert _normalize(_raw("om_2", msgType="post")).message_type == "post"
    assert (
        _normalize(
            _raw("om_3", content={"text": "x", "message_type": "image"})
        ).message_type
        == "image"
    )
    assert _normalize(_raw("om_4")).message_type is None


def test_merge_forward_is_a_container_with_placeholder_resources() -> None:
    message = _normalize(
        _raw(
            "om_fwd",
            chat_type="p2p",
            msg_type="merge_forward",
            text=(
                "<forwarded_messages>\n"
                "[2026-07-08T15:24:09+08:00] A:\n  deployment question\n"
                "[Image: img_fwd_1]\n"
                "</forwarded_messages>"
            ),
        )
    )
    assert message.message_type == "merge_forward"
    assert message.resources == []
    assert "deployment question" in message.text


def test_reaction_payload_is_not_a_signal() -> None:
    base = _raw("om_react")
    with_reactions = {
        **base,
        "reactions": [
            {
                "type": "emoji",
                "emoji_type": "THUMBSUP",
                "user_id_list": ["ou_owner"],
            }
        ],
    }
    plain = _normalize(base)
    reacted = _normalize(with_reactions)
    # Reaction presence never changes signal-relevant fields: no mention is
    # derived from a reaction, and message identity stays identical.
    assert reacted.direct_mention is False
    assert reacted.message_type == plain.message_type
    assert reacted.direct_mention == plain.direct_mention
    assert reacted.reply_to_message_id == plain.reply_to_message_id
    assert reacted.sender_role == plain.sender_role


def test_store_persists_message_type(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    message = _normalize(_raw("om_1", msg_type="merge_forward"))
    store.upsert_message(message)
    row = store.get_message("om_1")
    assert row is not None
    assert row["message_type"] == "merge_forward"


def test_store_updates_message_type_on_reupsert(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.upsert_message(_normalize(_raw("om_1", msg_type="image")))
    store.upsert_message(_normalize(_raw("om_1", msg_type="file", text="edited")))
    row = store.get_message("om_1")
    assert row is not None
    assert row["message_type"] == "file"


def test_schema_version_4_includes_message_type_column(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.initialize()
    with store.connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    assert version == 4
    assert "message_type" in columns


def test_message_detail_exposes_message_type(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.upsert_message(_normalize(_raw("om_1", msg_type="image")))
    query = MessageDetailQuery(
        connect=store.connect, now=lambda: "2026-06-22T10:10:00+08:00"
    )
    detail = query.message_detail("om_1")
    assert detail is not None
    assert detail["message"]["message_type"] == "image"


def test_cross_chat_reply_to_never_matches_candidates(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    root = _normalize(_raw("om_root", chat_id="oc_B"))
    store.upsert_message(root)
    chat_b_task, _ = store.create_task_for_message_and_audit(
        root, watch_until="2026-06-22T12:10:00+08:00"
    )

    # Reply target message id lives in chat B; the reply arrives in chat A and
    # carries a direct mention so it passes the non-direct-mention gate and
    # actually reaches candidate collection.
    reply = _normalize(
        _raw(
            "om_reply",
            chat_id="oc_A",
            reply_to="om_root",
            mentions=[{"open_id": "ou_owner"}],
        )
    )
    now = "2026-06-22T10:10:00+08:00"
    candidates = CandidateCollector(store).collect(reply, now=now)
    assert all(candidate.matched_by != "reply_to_msg" for candidate in candidates)

    result = MessageRouter(store=store).route(
        message=reply,
        source="group_at_me",
        inserted=True,
        now=now,
        watch_until="2026-06-22T12:10:00+08:00",
    )
    # Routing never crosses chat boundaries: the cross-chat reply is handled as
    # chat-A work (a new chat-A task), never attached to chat B's task.
    assert result.task is not None
    assert result.task.chat_id == "oc_A"
    assert result.task.id != chat_b_task.id
    with store.connect() as conn:
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (chat_b_task.id,)
        ).fetchone()
    assert status["status"] == "watching"


def test_owner_takeover_does_not_cross_chat_boundary(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    root = _normalize(_raw("om_root", chat_id="oc_B"))
    store.upsert_message(root)
    task, _ = store.create_task_for_message_and_audit(
        root, watch_until="2026-06-22T12:10:00+08:00"
    )
    router = MessageRouter(store=store)

    owner_cross_chat = _normalize(
        _raw(
            "om_owner_a",
            chat_id="oc_A",
            sender_id="ou_owner",
            reply_to="om_root",
        )
    )
    result = router.route(
        message=owner_cross_chat,
        source="group_at_me",
        inserted=True,
        now="2026-06-22T10:10:00+08:00",
        watch_until="2026-06-22T12:10:00+08:00",
    )
    assert result.task is None
    assert result.decision.reason == "owner_message_not_task_intervention"

    owner_same_chat = _normalize(
        _raw(
            "om_owner_b",
            chat_id="oc_B",
            sender_id="ou_owner",
            reply_to="om_root",
        )
    )
    result = router.route(
        message=owner_same_chat,
        source="group_at_me",
        inserted=True,
        now="2026-06-22T10:10:00+08:00",
        watch_until="2026-06-22T12:10:00+08:00",
    )
    assert result.task is not None
    assert result.task.id == task.id
