from __future__ import annotations

from pathlib import Path

import pytest

from feishu_shadow_agent.config import ConfigService
from feishu_shadow_agent.evals.artifacts import (
    EvalError,
    read_jsonl,
    read_yaml,
    write_jsonl,
    write_yaml,
)
from feishu_shadow_agent.evals.ingress import (
    GroupIngressEvaluator,
    build_review_labels,
    compare_ingress_golden,
)
from feishu_shadow_agent.evals.service import EvalService
from feishu_shadow_agent.types import LarkCliResult, MessagePage


class PagedIngressClient:
    def list_chat_messages(self, *, page_token=None, **kwargs):
        if page_token is None:
            return MessagePage(
                [_message_with_id("om_1", minute=1, direct=False)],
                next_page_token="chat-2",
                has_more=True,
            )
        assert page_token == "chat-2"
        return MessagePage([_message_with_id("om_2", minute=2, direct=True)])

    def search_messages(self, *, page_token=None, **kwargs):
        if page_token is None:
            return MessagePage([], next_page_token="search-2", has_more=True)
        assert page_token == "search-2"
        return MessagePage([_message_with_id("om_2", minute=2, direct=True)])

    def version(self):
        return LarkCliResult(argv=["lark-cli", "--version"], exit_code=0)


def test_group_ingress_timeline_marks_direct_mention_kept() -> None:
    raw = _message(direct=True)

    timeline = GroupIngressEvaluator(owner_open_id="ou_owner").build_timeline(
        [raw],
        sources_by_message={"om_1": {"group_at_me"}},
        owner={"open_id": "ou_owner", "name": "Owner"},
        chat={"chat_id": "oc_test"},
    )

    current = timeline["messages"][0]
    assert current["current_decision"] == "kept"
    assert current["reason_code"] == "direct_owner_mention"
    assert current["sources"] == ["group_at_me"]


def test_live_ingress_paginates_fixed_window_and_freezes_group_at_source(
    tmp_path: Path,
) -> None:
    loaded = _loaded_config(tmp_path)
    service = EvalService(loaded=loaded, lark_client=PagedIngressClient())

    run_dir = service.run_ingress(
        chat_id="oc_test",
        snapshot=None,
        start="2026-07-10T10:00:00+08:00",
        end="2026-07-10T11:00:00+08:00",
        lookback_days=None,
        label=None,
        dry_run_backend=True,
        allow_sensitive_config=False,
    )

    assert [
        row["message_id"] for row in read_jsonl(run_dir / "raw_messages.jsonl")
    ] == [
        "om_1",
        "om_2",
    ]
    timeline = read_yaml(run_dir / "ingress_timeline.yaml")
    assert timeline["chat"]["start"] == "2026-07-10T10:00:00+08:00"
    assert timeline["chat"]["end"] == "2026-07-10T11:00:00+08:00"
    by_id = {row["message_id"]: row for row in timeline["messages"]}
    assert by_id["om_1"]["sources"] == []
    assert by_id["om_2"]["sources"] == ["group_at_me"]


def test_ingress_review_labels_sort_mismatches_first() -> None:
    timeline = {
        "run_id": "ingress-test",
        "messages": [
            _timeline_row(1, "om_1"),
            _timeline_row(2, "om_2"),
        ],
    }

    labels = build_review_labels(
        timeline,
        judge_labels={
            "om_2": {
                "expected_decision": "kept",
                "review_reason": "context follow-up",
            }
        },
    )

    assert [row["message_id"] for row in labels["labels"]] == ["om_2", "om_1"]
    assert labels["labels"][0]["review_reason"] == "context follow-up"
    assert labels["labels"][1]["review_reason"] == ""


def test_ingress_directory_snapshot_promote_and_golden_replay(
    tmp_path: Path,
) -> None:
    loaded = _loaded_config(tmp_path)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    raw = _message(direct=False)
    write_jsonl(snapshot / "raw_messages.jsonl", [raw])
    write_yaml(
        snapshot / "eval_case.yaml",
        {
            "schema_version": "eval_case_v1",
            "case_type": "ingress",
            "acquisition": {"active_tasks": {}},
        },
    )
    timeline = GroupIngressEvaluator(owner_open_id="ou_owner").build_timeline(
        [raw],
        sources_by_message={"om_1": set()},
        owner={"open_id": "ou_owner"},
        chat={"chat_id": "oc_test", "start": "a", "end": "b"},
    )
    write_yaml(snapshot / "ingress_timeline.yaml", timeline)
    (snapshot / "config.yaml").write_text(
        loaded.path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    write_yaml(snapshot / "metadata.yaml", {"schema_version": "eval_metadata_v1"})
    service = EvalService(loaded=loaded)

    run_dir = service.run_ingress(
        chat_id=None,
        snapshot=snapshot,
        start=None,
        end=None,
        lookback_days=None,
        label="snapshot-test",
        dry_run_backend=True,
        allow_sensitive_config=False,
    )
    labels_path = run_dir / "labels.review.yaml"
    labels = read_yaml(labels_path)
    labels["labels"][0]["expected_decision"] = "kept"
    labels["labels"][0]["review_reason"] = "manual context follow-up"
    write_yaml(labels_path, labels)

    golden = service.promote(
        eval_type="ingress",
        run_dir=run_dir,
        case_dir=None,
        review_path=labels_path,
        name="ingress-golden-test",
    )

    assert len(read_jsonl(golden / "raw_messages.jsonl")) == 1
    assert not (golden / "store_snapshot.sqlite3").exists()
    replay_dir, exit_code = service.run_ingress_golden(case_dir=golden, label="replay")
    assert exit_code == 1
    report = read_yaml(replay_dir / "report.yaml")
    assert report["summary"]["false_negative"] == 1


def test_compare_ingress_golden_passes_when_decisions_match() -> None:
    timeline = {"messages": [_timeline_row(1, "om_1")]}
    labels = {
        "labels": [
            {
                "message_id": "om_1",
                "expected_decision": "dropped",
                "review_reason": "",
            }
        ]
    }

    report = compare_ingress_golden(timeline=timeline, labels=labels)

    assert report["summary"]["passed"] is True
    assert report["mismatches"] == []


def test_compare_ingress_golden_rejects_duplicate_message_labels() -> None:
    timeline = {"messages": [_timeline_row(1, "om_1")]}
    labels = {
        "labels": [
            {
                "message_id": "om_1",
                "expected_decision": "dropped",
                "review_reason": "",
            },
            {
                "message_id": "om_1",
                "expected_decision": "kept",
                "review_reason": "conflict",
            },
        ]
    }

    with pytest.raises(EvalError, match="exactly once"):
        compare_ingress_golden(timeline=timeline, labels=labels)


def test_ingress_golden_rejects_sensitive_config_before_creating_run(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        Path("tests/fixtures/minimal.config.yaml")
        .read_text(encoding="utf-8")
        .replace(
            "chats:\n",
            "chats:\n  access-token:\n    name: Sensitive marker\n",
        ),
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)

    with pytest.raises(EvalError, match="sensitive fields"):
        EvalService(loaded=loaded).run_ingress_golden(
            case_dir=tmp_path / "missing-golden",
            label=None,
        )

    assert not (tmp_path / "data/evals/runs/ingress").exists()


def _loaded_config(tmp_path: Path):
    source = Path("tests/fixtures/minimal.config.yaml")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return ConfigService().load(config_path)


def _message(*, direct: bool) -> dict:
    return _message_with_id("om_1", minute=0, direct=direct)


def _message_with_id(message_id: str, *, minute: int, direct: bool) -> dict:
    return {
        "message_id": message_id,
        "chat_id": "oc_test",
        "chat_type": "group",
        "sender_id": "ou_user",
        "sender_name": "User",
        "create_time": f"2026-07-10T10:{minute:02d}:00+08:00",
        "text": "@Owner help" if direct else "follow-up without mention",
        "mentions": [{"open_id": "ou_owner"}] if direct else [],
    }


def _timeline_row(index: int, message_id: str) -> dict:
    return {
        "index": index,
        "message_id": message_id,
        "sent_at": f"2026-07-10T10:0{index}:00+08:00",
        "sender_name": "User",
        "text": "noise",
        "sources": [],
        "current_decision": "dropped",
        "reason_code": "not_acquired",
    }
