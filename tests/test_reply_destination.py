from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from feishu_shadow_agent.ingestion import MessageNormalizer
from feishu_shadow_agent.reply_destination import (
    candidate_from_message,
    message_location,
)
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


@pytest.mark.parametrize(
    ("raw", "thread_id", "locations"),
    [
        ({"message_position": "42"}, None, ("main",)),
        (
            {"message_position": "42", "thread_message_position": "-1"},
            "omt_1",
            ("main", "thread"),
        ),
        (
            {"message_position": "-3", "thread_message_position": "0"},
            "omt_1",
            ("thread",),
        ),
        ({}, "omt_1", ()),
        ({"message_position": "2", "thread_message_position": "1"}, "omt_1", ()),
        ({"message_position": "bad", "thread_message_position": "0"}, "omt_1", ()),
        ({"message_position": "42", "thread_message_position": "bad"}, "omt_1", ()),
        ({"message_position": True}, None, ()),
        (
            {"message_position": None, "thread_message_position": "0"},
            "omt_1",
            ("thread",),
        ),
    ],
)
def test_candidate_requires_position_evidence(
    raw: dict[str, Any], thread_id: str | None, locations: tuple[str, ...]
) -> None:
    row = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "thread_id": thread_id,
        "raw_json": raw,
    }
    candidate = candidate_from_message(row, expected_chat_id="oc_1", chat_type="group")
    assert candidate is not None and candidate.locations == locations
    assert (
        candidate_from_message(row, expected_chat_id="oc_other", chat_type="group")
        is None
    )
    if len(locations) != 1:
        assert candidate.destination(None) is None
    if "thread" in locations:
        assert candidate.destination("thread") == {
            "chat_id": "oc_1",
            "location": "thread",
            "thread_id": "omt_1",
        }


def test_readback_position_does_not_treat_thread_id_as_location() -> None:
    assert message_location({"thread_id": "omt_1"}, chat_type="group") is None
    assert (
        message_location(
            {
                "thread_id": "omt_1",
                "message_position": "42",
                "thread_message_position": "-1",
            },
            chat_type="group",
        )
        == "main"
    )
    assert (
        message_location(
            {
                "thread_id": "omt_1",
                "message_position": "-3",
                "thread_message_position": "0",
            },
            chat_type="group",
        )
        == "thread"
    )
    assert (
        message_location(
            {"message_position": "bad", "thread_message_position": "0"},
            chat_type="group",
        )
        is None
    )
    assert message_location({"message_position": False}, chat_type="p2p") is None


def test_position_metadata_is_enriched_without_advancing_revision_and_conflicts_stop_send(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    normalizer = MessageNormalizer(owner_open_id="ou_owner")
    raw = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "thread_id": "omt_1",
        "content": {"text": "hello"},
    }
    store.upsert_message(normalizer.normalize(raw))
    first_position = normalizer.normalize(
        raw | {"message_position": "42", "thread_message_position": "-1"}
    )
    enriched = store.upsert_message_with_revision(first_position)
    row = store.get_message("om_1")
    assert not enriched.changed and enriched.revision == 1 and row is not None
    candidate = candidate_from_message(
        dict(row), expected_chat_id="oc_1", chat_type="group"
    )
    assert candidate is not None and candidate.locations == ("main", "thread")

    conflicting = normalizer.normalize(
        raw | {"message_position": "-3", "thread_message_position": "0"}
    )
    conflict_result = store.upsert_message_with_revision(conflicting)
    assert not conflict_result.changed and conflict_result.requires_confirmation
    row = store.get_message("om_1")
    assert row is not None
    candidate = candidate_from_message(
        dict(row), expected_chat_id="oc_1", chat_type="group"
    )
    assert candidate is not None and candidate.locations == ()

    assert not store.upsert_message_with_revision(
        conflicting, confirmed_current=True
    ).changed
    row = store.get_message("om_1")
    assert row is not None
    candidate = candidate_from_message(
        dict(row), expected_chat_id="oc_1", chat_type="group"
    )
    assert candidate is not None and candidate.locations == ("thread",)


def test_position_conflict_across_edits_requires_current_snapshot(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    normalizer = MessageNormalizer(owner_open_id="ou_owner")
    raw = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "thread_id": "omt_1",
        "content": {"text": "before"},
        "message_position": "42",
        "thread_message_position": "-1",
    }
    store.upsert_message(normalizer.normalize(raw))
    edited = normalizer.normalize(
        raw
        | {
            "content": {"text": "after"},
            "message_position": "-3",
            "thread_message_position": "0",
        }
    )
    conflict = store.upsert_message_with_revision(edited)
    row = store.get_message("om_1")
    assert conflict.requires_confirmation and conflict.revision == 1 and row is not None
    candidate = candidate_from_message(
        dict(row), expected_chat_id="oc_1", chat_type="group"
    )
    assert candidate is not None and candidate.locations == ()
    confirmed = store.upsert_message_with_revision(edited, confirmed_current=True)
    row = store.get_message("om_1")
    assert confirmed.changed and confirmed.revision == 2 and row is not None
    candidate = candidate_from_message(
        dict(row), expected_chat_id="oc_1", chat_type="group"
    )
    assert candidate is not None and candidate.locations == ("thread",)


def test_null_position_can_be_enriched_without_a_false_conflict(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    normalizer = MessageNormalizer(owner_open_id="ou_owner")
    raw = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "thread_id": "omt_1",
        "content": {"text": "hello"},
        "message_position": None,
        "thread_message_position": "-1",
    }
    store.upsert_message(normalizer.normalize(raw))
    result = store.upsert_message_with_revision(
        normalizer.normalize(raw | {"message_position": "42"})
    )
    row = store.get_message("om_1")
    assert not result.requires_confirmation and row is not None
    candidate = candidate_from_message(
        dict(row), expected_chat_id="oc_1", chat_type="group"
    )
    assert candidate is not None and candidate.locations == ("main", "thread")
