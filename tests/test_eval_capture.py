from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from feishu_shadow_agent.config import ConfigService
from feishu_shadow_agent.evals.artifacts import (
    EvalError,
    read_jsonl,
    read_yaml,
    write_yaml,
)
from feishu_shadow_agent.evals.service import EvalService
from feishu_shadow_agent.ingestion import MessageNormalizer
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import LarkCliResult, MessagePage


class FakeLarkClient:
    def __init__(self, messages: list[dict]):
        self.messages = messages

    def get_messages(self, *, as_identity: str, message_ids: list[str]):
        return MessagePage(
            [row for row in self.messages if row["message_id"] in message_ids]
        )

    def list_chat_messages(self, **kwargs):
        return MessagePage(self.messages)

    def search_messages(self, *, chat_type: str, **kwargs):
        return MessagePage(
            [row for row in self.messages if row["chat_type"] == chat_type]
        )

    def version(self):
        return LarkCliResult(argv=["lark-cli", "version"], exit_code=0, stdout="1.0")

    def download_resource(self, **kwargs):
        raise AssertionError("test messages have no resources")


def test_capture_writes_flat_runnable_reviews_and_promotes_router(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        Path("tests/fixtures/minimal.config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    messages = [
        _message("om_1", minute=1, direct=False),
        _message("om_2", minute=2, direct=True),
    ]
    service = EvalService(loaded=loaded, lark_client=FakeLarkClient(messages))

    case = service.capture_case(
        message_id="om_2",
        context_before=1,
        context_after=0,
        lookback_days=2,
        label="capture-test",
        allow_sensitive_config=False,
    )

    assert not (case / "eval_case.yaml").exists()
    assert not (case / "store_snapshot.sqlite3").exists()
    assert [row["message_id"] for row in read_jsonl(case / "messages.jsonl")] == [
        "om_1",
        "om_2",
    ]
    for name in (
        "router.review.yaml",
        "task_session.review.yaml",
        "full_chain.review.yaml",
    ):
        assert (case / name).is_file()
    assert (
        read_yaml(case / "task_session.review.yaml")["labels"]["expected_skills"] == []
    )

    run_dir, exit_code = service.run_router(
        case_dir=case, label=None, dry_run_backend=True
    )
    assert exit_code == 0
    assert read_yaml(run_dir / "report.yaml")["passed"] is None

    review_path = case / "router.review.yaml"
    review = read_yaml(review_path)
    review["labels"] = {
        "route": "new_task",
        "task_key": None,
    }
    write_yaml(review_path, review)
    golden = service.promote(
        eval_type="router",
        run_dir=None,
        case_dir=case,
        review_path=review_path,
        name="captured-router",
    )

    assert (golden / "eval_case.yaml").is_file()
    assert (golden / "labels.yaml").is_file()
    assert (golden / "provenance.yaml").is_file()
    assert not (golden / "router.review.yaml").exists()


def test_task_session_promotion_preserves_expected_skills(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        Path("tests/fixtures/minimal.config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    message = _message("om_1", minute=1, direct=True)
    service = EvalService(loaded=loaded, lark_client=FakeLarkClient([message]))
    case = service.capture_case(
        message_id="om_1",
        context_before=0,
        context_after=0,
        lookback_days=2,
        label="task-session-skills",
        allow_sensitive_config=False,
    )
    review_path = case / "task_session.review.yaml"
    review = read_yaml(review_path)
    review["labels"] = {
        "answerability": "no_reply",
        "decision_reason": "no_response_needed",
        "watch_action": "keep_watching",
        "expected_skills": ["docmate"],
    }
    write_yaml(review_path, review)

    golden = service.promote(
        eval_type="task-session",
        run_dir=None,
        case_dir=case,
        review_path=review_path,
        name="task-session-skills",
    )

    assert read_yaml(golden / "labels.yaml")["expected_skills"] == ["docmate"]


def test_capture_candidates_deduplicates_union(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        Path("tests/fixtures/minimal.config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    messages = [
        _message("om_group", minute=1, direct=True),
        {
            **_message("om_p2p", minute=2, direct=False),
            "chat_type": "p2p",
        },
    ]
    service = EvalService(loaded=loaded, lark_client=FakeLarkClient(messages))

    rows = service.capture_candidates(lookback_days=2, limit=20)

    assert [row["message_id"] for row in rows] == ["om_p2p", "om_group"]
    assert [row["sources"] for row in rows] == [["p2p"], ["group_at_me"]]


def test_capture_candidates_fetches_all_pages_before_global_limit(
    tmp_path: Path,
) -> None:
    loaded = _loaded_config(tmp_path)
    pages = {
        None: MessagePage(
            [_message("om_old_1", minute=1, direct=True)],
            next_page_token="page-2",
            has_more=True,
        ),
        "page-2": MessagePage(
            [
                _message("om_new", minute=3, direct=True),
                _message("om_old_2", minute=2, direct=True),
            ]
        ),
    }

    class PagedClient(FakeLarkClient):
        def search_messages(self, *, chat_type: str, page_token=None, **kwargs):
            if chat_type == "p2p":
                return MessagePage([])
            return pages[page_token]

    rows = EvalService(loaded=loaded, lark_client=PagedClient([])).capture_candidates(
        lookback_days=2, limit=2
    )

    assert [row["message_id"] for row in rows] == ["om_new", "om_old_2"]


def test_capture_candidates_use_second_precision_search_window(
    tmp_path: Path,
) -> None:
    loaded = _loaded_config(tmp_path)
    windows: list[tuple[str, str]] = []

    class RecordingClient(FakeLarkClient):
        def search_messages(self, *, start: str, end: str, **kwargs):
            windows.append((start, end))
            return MessagePage([])

    EvalService(loaded=loaded, lark_client=RecordingClient([])).capture_candidates(
        lookback_days=7, limit=20
    )

    assert len(windows) == 2
    assert all(
        datetime.fromisoformat(value).microsecond == 0
        for pair in windows
        for value in pair
    )


def test_capture_accepts_lark_cli_local_timestamp(tmp_path: Path) -> None:
    loaded = _loaded_config(tmp_path)
    message = _message("om_local", minute=1, direct=True)
    message["create_time"] = "2026-07-10 10:01"
    service = EvalService(loaded=loaded, lark_client=FakeLarkClient([message]))

    case = service.capture_case(
        message_id="om_local",
        context_before=0,
        context_after=0,
        lookback_days=2,
        label="local-time",
        allow_sensitive_config=False,
    )
    run_dir, exit_code = service.run_task_session(
        case_dir=case,
        label=None,
        repeat=1,
        dry_run_backend=True,
    )

    assert exit_code == 0
    assert (run_dir / "report.yaml").is_file()
    assert read_jsonl(case / "messages.jsonl")[0]["create_time"] == "2026-07-10 10:01"


def test_capture_uses_relative_resource_output(tmp_path: Path) -> None:
    loaded = _loaded_config(tmp_path)
    message = _message("om_image", minute=1, direct=True)
    message["text"] += " ![Image](img_fixture)"
    outputs: list[str] = []

    class ResourceClient(FakeLarkClient):
        def download_resource(self, *, output: str, **kwargs):
            outputs.append(output)
            destination = loaded.base_dir / output
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"image fixture")
            return LarkCliResult(argv=[], exit_code=0)

    service = EvalService(loaded=loaded, lark_client=ResourceClient([message]))
    case = service.capture_case(
        message_id="om_image",
        context_before=0,
        context_after=0,
        lookback_days=2,
        label="relative-resource",
        allow_sensitive_config=False,
    )

    assert len(outputs) == 1
    assert not Path(outputs[0]).is_absolute()
    assert not read_yaml(case / "metadata.yaml").get("resource_capture_errors")
    assert (
        len(read_yaml(case / "task_session.review.yaml")["scenario"]["resources"]) == 1
    )


def test_capture_resolves_p2p_source_when_mget_omits_chat_type(
    tmp_path: Path,
) -> None:
    loaded = _loaded_config(tmp_path)
    seed = _message("om_p2p", minute=1, direct=False)
    seed.pop("chat_type")
    searched = seed | {"chat_type": "p2p"}

    class SourceClient(FakeLarkClient):
        def search_messages(self, *, chat_id=None, chat_type=None, **kwargs):
            assert chat_id == "oc_test"
            assert chat_type is None
            return MessagePage([searched])

    service = EvalService(loaded=loaded, lark_client=SourceClient([seed]))
    case = service.capture_case(
        message_id="om_p2p",
        context_before=0,
        context_after=0,
        lookback_days=2,
        label="p2p-source",
        allow_sensitive_config=False,
    )

    router = read_yaml(case / "router.review.yaml")
    full_chain = read_yaml(case / "full_chain.review.yaml")
    assert router["scenario"]["target"]["source"] == "p2p"
    assert full_chain["scenario"]["target"]["source"] == "p2p"


def test_capture_reads_minimal_task_fixture_from_production_store(
    tmp_path: Path,
) -> None:
    loaded = _loaded_config(tmp_path)
    first = _message("om_1", minute=1, direct=True)
    target = _message("om_2", minute=2, direct=True)
    store = SQLiteStore(tmp_path / "data/test.sqlite3")
    store.migrate()
    normalized = MessageNormalizer(owner_open_id="ou_owner").normalize(first)
    store.upsert_message(normalized)
    store.create_task_for_message(
        normalized,
        watch_until="2026-07-10T12:00:00+08:00",
        task_label="captured task",
    )
    service = EvalService(loaded=loaded, lark_client=FakeLarkClient([first, target]))

    case = service.capture_case(
        message_id="om_2",
        context_before=0,
        context_after=0,
        lookback_days=2,
        label=None,
        allow_sensitive_config=False,
    )

    review = read_yaml(case / "router.review.yaml")
    assert review["scenario"]["tasks"] == {
        "task_1": {
            "status": "watching",
            "task_label": "captured task",
            "message_ids": ["om_1"],
        }
    }


def test_promote_rechecks_sensitive_source_config(tmp_path: Path) -> None:
    loaded = _loaded_config(tmp_path)
    messages = [_message("om_1", minute=1, direct=True)]
    service = EvalService(loaded=loaded, lark_client=FakeLarkClient(messages))
    case = service.capture_case(
        message_id="om_1",
        context_before=0,
        context_after=0,
        lookback_days=2,
        label=None,
        allow_sensitive_config=False,
    )
    review_path = case / "router.review.yaml"
    review = read_yaml(review_path)
    review["labels"] = {"route": "new_task", "task_key": None}
    write_yaml(review_path, review)
    copied_config = read_yaml(case / "config.yaml")
    copied_config["local_access_token"] = "secret-value"
    write_yaml(case / "config.yaml", copied_config)

    with pytest.raises(EvalError, match="sensitive fields"):
        service.promote(
            eval_type="router",
            run_dir=None,
            case_dir=case,
            review_path=review_path,
            name="sensitive-router",
        )


def test_promote_rejects_expired_watching_router_fixture(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        Path("tests/fixtures/minimal.config.yaml")
        .read_text(encoding="utf-8")
        .replace("watch_minutes: 120", "watch_minutes: 1"),
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    first = _message("om_1", minute=1, direct=True)
    target = _message("om_2", minute=3, direct=True)
    service = EvalService(loaded=loaded, lark_client=FakeLarkClient([first, target]))
    case = service.capture_case(
        message_id="om_2",
        context_before=1,
        context_after=0,
        lookback_days=2,
        label=None,
        allow_sensitive_config=False,
    )
    review_path = case / "router.review.yaml"
    review = read_yaml(review_path)
    review["scenario"]["tasks"] = {
        "task_1": {
            "status": "watching",
            "task_label": "expired",
            "message_ids": ["om_1"],
        }
    }
    review["labels"] = {"route": "attach_task", "task_key": "task_1"}
    write_yaml(review_path, review)

    with pytest.raises(EvalError, match="expired at target"):
        service.promote(
            eval_type="router",
            run_dir=None,
            case_dir=case,
            review_path=review_path,
            name="expired-router",
        )


def _loaded_config(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        Path("tests/fixtures/minimal.config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return ConfigService().load(config_path)


def _message(message_id: str, *, minute: int, direct: bool) -> dict:
    return {
        "message_id": message_id,
        "chat_id": "oc_test",
        "chat_type": "group",
        "sender_id": "ou_user",
        "sender_name": "User",
        "create_time": f"2026-07-10T10:{minute:02d}:00+08:00",
        "text": "@Owner help" if direct else "context",
        "mentions": [{"open_id": "ou_owner"}] if direct else [],
    }
