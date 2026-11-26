from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from feishu_shadow_agent.cli import main
from feishu_shadow_agent.evals.artifacts import EvalError
from feishu_shadow_agent.evals.coworker_dataset import (
    CoworkerReviewDataset,
    inspect_coworker_review,
    render_coworker_review,
    require_coworker_review_approval,
)
from feishu_shadow_agent.evals.coworker_repositories import (
    materialize_coworker_repositories,
)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _dataset(root: Path) -> dict[str, Any]:
    (root / "resources").mkdir()
    (root / "raw").mkdir()
    (root / "evidence").mkdir()
    (root / "resources" / "safe.log").write_text("redacted log", encoding="utf-8")
    (root / "raw" / "source.log").write_text("private raw data", encoding="utf-8")
    (root / "evidence" / "source.go").write_text("source snapshot", encoding="utf-8")
    return {
        "schema_version": "coworker_comparison_review_v1",
        "dataset_id": "example",
        "state": "draft_not_approved",
        "window_start": "2026-11-17T00:00:00+08:00",
        "window_end": "2026-11-24T11:08:00+08:00",
        "rules": [],
        "cases": [
            {
                "case_id": "G01",
                "title": "example",
                "source_kind": "group_work",
                "source_review": {
                    "message_id": "private-source-id",
                    "chat_id": "private-chat-id",
                    "source_time": "2026-11-20 09:30",
                    "source_app_link": None,
                    "current_snapshot_edited": False,
                    "snapshot_update_time": None,
                    "original_text": "private original",
                    "context_source_message_ids": ["private-history-id"],
                    "resources": [
                        {
                            "source_message_id": "private-history-id",
                            "raw_local_path": "raw/source.log",
                            "raw_sha256": _sha(
                                (root / "raw" / "source.log").read_bytes()
                            ),
                            "model_input_path": "resources/safe.log",
                            "excluded_from_model": False,
                            "source_timing": "prior",
                        }
                    ],
                },
                "scenario": {
                    "chat_type": "group",
                    "trigger": "direct_mention",
                    "message": "@测试机器人 help",
                    "history_available_to_tools": [
                        {
                            "role": "同事",
                            "timestamp": "2026-11-20 09:29",
                            "text": "history",
                        }
                    ],
                    "quoted_message": None,
                    "assets": [
                        {
                            "path": "resources/safe.log",
                            "sha256": _sha(
                                (root / "resources" / "safe.log").read_bytes()
                            ),
                            "kind": "file",
                            "access": "read only",
                        }
                    ],
                    "read_only_evidence": [
                        {
                            "path": "evidence/source.go",
                            "sha256": _sha(
                                (root / "evidence" / "source.go").read_bytes()
                            ),
                            "repository": "/private/repo",
                            "revision": "revision",
                            "revision_committed_at": "2026-11-19T12:00:00+08:00",
                            "source_path": "source.go",
                            "scope": "historical snapshot",
                        }
                    ],
                    "linked_topic_fixture": [],
                },
                "expected": {
                    "trigger": "reply_or_task",
                    "criteria": ["private expected answer"],
                    "reference_status": "draft",
                },
                "audit": {
                    "assistant_review": "reviewed_draft",
                    "review_note": "checked",
                    "human_review": "pending",
                },
            }
        ],
    }


def _write_review(root: Path, data: dict[str, Any]) -> Path:
    dataset_path = root / "dataset.review.yaml"
    dataset_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    dataset = CoworkerReviewDataset.model_validate(data)
    (root / "REVIEW.md").write_text(
        render_coworker_review(dataset, preamble="# Review\n\n"), encoding="utf-8"
    )
    paths = {"dataset.review.yaml", "REVIEW.md"}
    paths.update(item.path for item in dataset.support_files)
    for case in dataset.cases:
        paths.update(item.path for item in case.scenario.assets)
        paths.update(item.path for item in case.scenario.read_only_evidence)
    (root / "SHA256SUMS.review").write_text(
        "".join(
            f"{_sha((root / path).read_bytes())}  {path}\n" for path in sorted(paths)
        ),
        encoding="utf-8",
    )
    return dataset_path


def test_replay_input_keeps_review_and_expected_values_out_of_the_event(
    tmp_path: Path,
) -> None:
    data = _dataset(tmp_path)
    data["cases"][0]["scenario"]["mode_config"] = {
        "codex_reply_mode": "auto",
        "codex_at_all_suppressed": True,
    }
    path = _write_review(tmp_path, data)
    inspected = inspect_coworker_review(path)
    payload = inspected.replay_input("G01")

    assert payload["event"]["message"] == "@测试机器人 help"
    assert "history" not in payload["event"]
    assert payload["tool_fixtures"]["history"][0]["text"] == "history"
    assert payload["tool_fixtures"]["assets"][0]["placement"] == {
        "kind": "history",
        "message_id": "history-001",
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "private-source-id" not in encoded
    assert "private-chat-id" not in encoded
    assert "private-history-id" not in encoded
    assert "private expected answer" not in encoded
    assert "codex_at_all_suppressed" not in encoded
    assert payload["mode_config"] == {"codex_reply_mode": "auto"}
    assert "/private/repo" not in encoded
    assert "raw/source.log" not in inspected.input_files
    assert inspected.summary()["real_model_authorized"] is False

    (tmp_path / "resources" / "safe.log").write_text("changed", encoding="utf-8")
    assert inspected.input_files["resources/safe.log"] == b"redacted log"


@pytest.mark.parametrize(
    ("changed_path", "message"),
    [
        ("resources/safe.log", "input hash mismatch"),
        ("raw/source.log", "source hash mismatch"),
        ("REVIEW.md", "does not match the dataset"),
    ],
)
def test_review_rejects_changed_inputs_or_review(
    tmp_path: Path, changed_path: str, message: str
) -> None:
    path = _write_review(tmp_path, _dataset(tmp_path))
    (tmp_path / changed_path).write_text("changed", encoding="utf-8")
    with pytest.raises(EvalError, match=message):
        inspect_coworker_review(path)


def test_review_rejects_context_after_target(tmp_path: Path) -> None:
    data = _dataset(tmp_path)
    data["cases"][0]["scenario"]["history_available_to_tools"][0]["timestamp"] = (
        "2026-11-20 09:31"
    )
    path = _write_review(tmp_path, data)
    with pytest.raises(EvalError, match="context is later than target"):
        inspect_coworker_review(path)


def test_review_allows_a_pinned_evaluation_time_repository_without_leaking_review(
    tmp_path: Path,
) -> None:
    data = _dataset(tmp_path)
    data["schema_version"] = "coworker_comparison_review_v2"
    case = data["cases"][0]
    case["scenario"]["read_only_repositories"] = [
        {
            "repository": "/private/source/repo",
            "mount_name": "repo",
            "revision": "a" * 40,
            "tree_sha1": "b" * 40,
            "revision_committed_at": "2026-11-23T12:00:00+08:00",
            "temporal_basis": "evaluation_time",
        }
    ]
    case["expected"]["reference_reply"] = "private reference answer"
    inspected = inspect_coworker_review(_write_review(tmp_path, data))
    payload = inspected.replay_input("G01")

    assert payload["tool_fixtures"]["repositories"] == [
        {"mount_name": "repo", "revision": "a" * 40, "tree_sha1": "b" * 40}
    ]
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "/private/source/repo" not in encoded
    assert "private reference answer" not in encoded


def test_review_freezes_common_support_without_leaking_skill_expectations(
    tmp_path: Path,
) -> None:
    data = _dataset(tmp_path)
    data["schema_version"] = "coworker_comparison_review_v2"
    support = tmp_path / "skill.md"
    support.write_text("common skill instructions", encoding="utf-8")
    data["support_files"] = [
        {
            "path": "skill.md",
            "sha256": _sha(support.read_bytes()),
            "install_path": "/sandbox/skills/example/SKILL.md",
        }
    ]
    data["cases"][0]["expected"]["skill_expectations"] = [
        {
            "name": "docmate",
            "requirement": "required",
            "rationale": "private reviewer rationale",
        }
    ]
    dataset_path = _write_review(tmp_path, data)
    inspected = inspect_coworker_review(dataset_path)
    payload = inspected.replay_input("G01")

    assert inspected.input_files["skill.md"] == b"common skill instructions"
    assert payload["tool_fixtures"]["support_files"] == data["support_files"]
    assert "private reviewer rationale" not in json.dumps(payload, ensure_ascii=False)
    assert "private reviewer rationale" in (tmp_path / "REVIEW.md").read_text()

    support.write_text("changed skill instructions", encoding="utf-8")
    with pytest.raises(EvalError, match="input hash mismatch"):
        inspect_coworker_review(dataset_path)


def test_model_approval_is_bound_to_the_inspected_snapshot(tmp_path: Path) -> None:
    review = inspect_coworker_review(_write_review(tmp_path, _dataset(tmp_path)))
    approval_path = tmp_path / "APPROVAL.yaml"
    approval = {
        "dataset_id": review.dataset.dataset_id,
        "review_snapshot_sha256": review.review_snapshot_sha256,
        "user_approved": True,
        "assistant_approved": True,
        "review_note": "reviewed",
    }
    approval_path.write_text(yaml.safe_dump(approval), encoding="utf-8")
    assert require_coworker_review_approval(review, approval_path).user_approved

    approval["review_snapshot_sha256"] = "a" * 64
    approval_path.write_text(yaml.safe_dump(approval), encoding="utf-8")
    with pytest.raises(EvalError, match="snapshot lacks both approvals"):
        require_coworker_review_approval(review, approval_path)

    approval["review_snapshot_sha256"] = review.review_snapshot_sha256
    approval["assistant_approved"] = False
    approval_path.write_text(yaml.safe_dump(approval), encoding="utf-8")
    with pytest.raises(EvalError, match="snapshot lacks both approvals"):
        require_coworker_review_approval(review, approval_path)


def test_review_rejects_excluded_raw_content_as_common_support(tmp_path: Path) -> None:
    data = _dataset(tmp_path)
    case = data["cases"][0]
    resource = case["source_review"]["resources"][0]
    resource["excluded_from_model"] = True
    resource["model_input_path"] = None
    case["scenario"]["assets"] = []
    data["support_files"] = [
        {
            "path": resource["raw_local_path"],
            "sha256": resource["raw_sha256"],
            "install_path": "/sandbox/skills/example/source.log",
        }
    ]
    path = _write_review(tmp_path, data)
    with pytest.raises(EvalError, match="excluded coworker source is a model input"):
        inspect_coworker_review(path)


def test_materialize_copies_pinned_git_tree_and_blocks_escaping_symlink(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-repo"
    source.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(source), *args],  # noqa: S607
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()

    git("init", "--quiet")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (source / "code.txt").write_text("committed version", encoding="utf-8")
    (source / "internal").symlink_to("code.txt")
    (source / "external").symlink_to("../private-secret")
    git("add", "code.txt", "internal", "external")
    git("commit", "--quiet", "-m", "fixture")
    submodule_revision = git("rev-parse", "HEAD")
    git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{submodule_revision},vendor/example",
    )
    git("commit", "--quiet", "-m", "add gitlink")
    revision = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    committed_at = git("show", "-s", "--format=%cI", "HEAD")
    (source / "code.txt").write_text("uncommitted version", encoding="utf-8")
    source_status = git("status", "--porcelain")

    data = _dataset(tmp_path)
    data["window_end"] = "2100-01-01T00:00:00+08:00"
    data["cases"][0]["scenario"]["read_only_repositories"] = [
        {
            "repository": str(source),
            "mount_name": "repo",
            "revision": revision,
            "tree_sha1": tree,
            "revision_committed_at": committed_at,
            "temporal_basis": "evaluation_time",
        }
    ]
    review = inspect_coworker_review(_write_review(tmp_path, data))
    destination = tmp_path / "isolated"
    (copied,) = materialize_coworker_repositories(review, "G01", destination)

    assert (copied.path / "code.txt").read_text() == "committed version"
    assert (copied.path / "internal").read_text() == "committed version"
    assert not (copied.path / "external").exists()
    assert copied.omitted_symlinks == ("external",)
    assert copied.uninitialized_submodules == ("vendor/example",)
    assert not (copied.path / ".git" / "FETCH_HEAD").exists()
    assert str(source) not in (copied.path / ".git" / "config").read_text()
    assert git("status", "--porcelain") == source_status

    with pytest.raises(EvalError, match="destination must be new"):
        materialize_coworker_repositories(review, "G01", destination)
    with pytest.raises(EvalError, match="outside source repos"):
        materialize_coworker_repositories(review, "G01", source / "copy")


def test_materialize_rejects_changed_tree_and_removes_partial_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-repo"
    source.mkdir()
    for args in (
        ("init", "--quiet"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
        ("commit", "--quiet", "--allow-empty", "-m", "fixture"),
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True)  # noqa: S603, S607
    revision = subprocess.check_output(  # noqa: S603
        ["git", "-C", str(source), "rev-parse", "HEAD"],  # noqa: S607
        text=True,
    ).strip()
    committed_at = subprocess.check_output(  # noqa: S603
        ["git", "-C", str(source), "show", "-s", "--format=%cI", "HEAD"],  # noqa: S607
        text=True,
    ).strip()
    data = _dataset(tmp_path)
    data["window_end"] = "2100-01-01T00:00:00+08:00"
    data["cases"][0]["scenario"]["read_only_repositories"] = [
        {
            "repository": str(source),
            "mount_name": "repo",
            "revision": revision,
            "tree_sha1": "a" * 40,
            "revision_committed_at": committed_at,
            "temporal_basis": "evaluation_time",
        }
    ]
    review = inspect_coworker_review(_write_review(tmp_path, data))
    destination = tmp_path / "isolated"
    with pytest.raises(EvalError, match="tree mismatch"):
        materialize_coworker_repositories(review, "G01", destination)
    assert not destination.exists()


def test_review_rejects_a_repository_revision_after_the_evaluation_window(
    tmp_path: Path,
) -> None:
    data = _dataset(tmp_path)
    data["schema_version"] = "coworker_comparison_review_v2"
    data["cases"][0]["scenario"]["read_only_repositories"] = [
        {
            "repository": "/private/source/repo",
            "mount_name": "repo",
            "revision": "a" * 40,
            "tree_sha1": "b" * 40,
            "revision_committed_at": "2026-11-25T12:00:00+08:00",
            "temporal_basis": "evaluation_time",
        }
    ]
    with pytest.raises(EvalError, match="evidence is later than evaluation window"):
        inspect_coworker_review(_write_review(tmp_path, data))


def test_review_binds_quoted_attachment_to_a_synthetic_message(tmp_path: Path) -> None:
    data = _dataset(tmp_path)
    case = data["cases"][0]
    case["scenario"]["quoted_message"] = {
        "role": "机器人",
        "timestamp": "2026-11-20 09:29",
        "text": "earlier reply",
    }
    case["source_review"]["context_source_message_ids"].append("private-quote-id")
    case["source_review"]["resources"][0]["source_message_id"] = "private-quote-id"
    inspected = inspect_coworker_review(_write_review(tmp_path, data))
    payload = inspected.replay_input("G01")

    assert payload["event"]["quoted_message"]["id"] == "quote-001"
    assert payload["tool_fixtures"]["assets"][0]["placement"] == {
        "kind": "quote",
        "message_id": "quote-001",
    }
    assert "private-quote-id" not in json.dumps(payload, ensure_ascii=False)


def test_review_binds_private_forwarded_asset_to_synthetic_event(
    tmp_path: Path,
) -> None:
    data = _dataset(tmp_path)
    case = data["cases"][0]
    case["source_kind"] = "private_work"
    case["scenario"]["chat_type"] = "private"
    case["scenario"]["trigger"] = "private_owner"
    case["scenario"]["forwarded_message"] = {
        "role": "同事",
        "message_type": "text",
        "text": "forwarded question",
    }
    case["scenario"]["history_available_to_tools"] = []
    inspected = inspect_coworker_review(_write_review(tmp_path, data))
    payload = inspected.replay_input("G01")

    assert payload["event"]["id"] == "event-001"
    assert payload["event"]["forwarded_message"]["text"] == "forwarded question"
    assert payload["tool_fixtures"]["assets"][0]["placement"] == {
        "kind": "event",
        "message_id": "event-001",
    }


def test_review_rejects_an_asset_without_a_replay_location(tmp_path: Path) -> None:
    data = _dataset(tmp_path)
    data["cases"][0]["source_review"]["resources"][0]["source_message_id"] = (
        "unrelated-source-id"
    )
    path = _write_review(tmp_path, data)
    with pytest.raises(EvalError, match="asset has no replay location"):
        inspect_coworker_review(path)


def test_review_rejects_target_outside_capture_window(tmp_path: Path) -> None:
    data = _dataset(tmp_path)
    data["cases"][0]["source_review"]["source_time"] = "2026-11-25 09:30"
    path = _write_review(tmp_path, data)
    with pytest.raises(EvalError, match="target is outside the review window"):
        inspect_coworker_review(path)


def test_review_rejects_excluded_raw_content_even_when_copied(tmp_path: Path) -> None:
    data = _dataset(tmp_path)
    case = data["cases"][0]
    source = dict(case["source_review"]["resources"][0])
    source["excluded_from_model"] = True
    source["model_input_path"] = None
    case["source_review"]["resources"].insert(0, source)
    copied = tmp_path / "resources" / "copied.log"
    copied.write_bytes((tmp_path / "raw" / "source.log").read_bytes())
    case["scenario"]["assets"].append(
        {
            "path": "resources/copied.log",
            "sha256": _sha(copied.read_bytes()),
            "kind": "file",
            "access": "read only",
        }
    )
    case["source_review"]["resources"].append(
        {
            "source_message_id": "private-history-id",
            "raw_local_path": "raw/source.log",
            "raw_sha256": _sha((tmp_path / "raw" / "source.log").read_bytes()),
            "model_input_path": "resources/copied.log",
            "excluded_from_model": False,
            "source_timing": "prior",
        }
    )
    path = _write_review(tmp_path, data)
    with pytest.raises(EvalError, match="excluded coworker source is a model input"):
        inspect_coworker_review(path)


def test_review_rejects_excluded_raw_content_as_code_evidence(tmp_path: Path) -> None:
    data = _dataset(tmp_path)
    case = data["cases"][0]
    source = dict(case["source_review"]["resources"][0])
    source["excluded_from_model"] = True
    source["model_input_path"] = None
    case["source_review"]["resources"].insert(0, source)
    copied = tmp_path / "evidence" / "copied.go"
    copied.write_bytes((tmp_path / "raw" / "source.log").read_bytes())
    evidence = case["scenario"]["read_only_evidence"][0]
    evidence["path"] = "evidence/copied.go"
    evidence["sha256"] = _sha(copied.read_bytes())
    path = _write_review(tmp_path, data)
    with pytest.raises(EvalError, match="excluded coworker source is a model input"):
        inspect_coworker_review(path)


def test_review_rejects_future_git_evidence_across_timezones(tmp_path: Path) -> None:
    data = _dataset(tmp_path)
    data["cases"][0]["scenario"]["read_only_evidence"][0]["revision_committed_at"] = (
        "2026-11-20T02:00:00+00:00"
    )
    path = _write_review(tmp_path, data)
    with pytest.raises(EvalError, match="evidence is later than target"):
        inspect_coworker_review(path)


def test_review_rejects_an_input_symlink_outside_the_dataset(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    root.mkdir()
    data = _dataset(root)
    outside = tmp_path / "outside.log"
    outside.write_text("outside", encoding="utf-8")
    link = root / "resources" / "escape.log"
    link.symlink_to(outside)
    asset = data["cases"][0]["scenario"]["assets"][0]
    asset["path"] = "resources/escape.log"
    asset["sha256"] = _sha(outside.read_bytes())
    data["cases"][0]["source_review"]["resources"][0]["model_input_path"] = (
        "resources/escape.log"
    )
    path = _write_review(root, data)
    with pytest.raises(EvalError, match="must stay inside the dataset"):
        inspect_coworker_review(path)


def test_review_cli_is_an_offline_inspection(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_review(tmp_path, _dataset(tmp_path))

    def reject_external_work(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("review inspection attempted external work")

    monkeypatch.setattr(socket.socket, "connect", reject_external_work)
    monkeypatch.setattr(subprocess, "Popen", reject_external_work)
    assert main(["eval", "inspect-coworker-review", "--dataset", str(path)]) == 0
    result = yaml.safe_load(capsys.readouterr().out)
    assert result["case_count"] == 1
    assert result["state"] == "draft_not_approved"
    assert result["real_model_authorized"] is False
