from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from feishu_shadow_agent.cli import main
from feishu_shadow_agent.evals.artifacts import read_yaml


def test_eval_help_includes_subcommands(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["eval", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "run-ingress" in output
    assert "run-task-session" in output
    assert "promote" in output


def test_eval_runner_requires_case_or_cases(capsys, tmp_path: Path) -> None:
    config_path = _copy_config(tmp_path)

    exit_code = main(
        [
            "eval",
            "run-router",
            "--config",
            str(config_path),
            "--dry-run-backend",
        ]
    )

    assert exit_code == 2
    assert "provide exactly one of --case or --cases" in capsys.readouterr().err


def test_eval_task_session_dry_run_draft_case(tmp_path: Path, capsys) -> None:
    config_path = _copy_config(tmp_path)
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "config.yaml").write_text(
        config_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    raw = _message("om_1", "2026-07-10T10:00:00+08:00")
    (case_dir / "messages.jsonl").write_text(
        json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _write_yaml(
        case_dir / "task_session.review.yaml",
        {
            "schema_version": "task_session_review_v1",
            "scenario": {
                "case_type": "task-session",
                "mode": "initial",
                "message_ids": ["om_1"],
                "resources": [],
            },
            "labels": {
                "reference_answer": None,
                "answerability": None,
                "decision_reason": None,
                "watch_action": None,
            },
        },
    )

    exit_code = main(
        [
            "eval",
            "run-task-session",
            "--config",
            str(config_path),
            "--case",
            str(case_dir),
            "--dry-run-backend",
        ]
    )

    assert exit_code == 0
    output = yaml.safe_load(capsys.readouterr().out)
    report = read_yaml(Path(output["report"]))
    assert report["label_status"] == "draft"
    assert report["passed"] is None


def test_eval_promote_requires_review_argument() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "eval",
                "promote",
                "--config",
                "config.yaml",
                "--type",
                "router",
                "--case",
                "case",
                "--name",
                "test",
            ]
        )

    assert exc_info.value.code == 2


def test_eval_warns_when_full_access_is_repeated(tmp_path: Path, capsys) -> None:
    config_path = _copy_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "tool_permissions: read_only", "tool_permissions: full_access"
        ),
        encoding="utf-8",
    )
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "config.yaml").write_text(
        config_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (case_dir / "messages.jsonl").write_text(
        json.dumps(
            _message("om_1", "2026-07-10T10:00:00+08:00"),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_yaml(
        case_dir / "task_session.review.yaml",
        {
            "schema_version": "task_session_review_v1",
            "scenario": {
                "case_type": "task-session",
                "mode": "initial",
                "message_ids": ["om_1"],
                "resources": [],
            },
            "labels": {},
        },
    )

    exit_code = main(
        [
            "eval",
            "run-task-session",
            "--config",
            str(config_path),
            "--case",
            str(case_dir),
            "--repeat",
            "2",
            "--dry-run-backend",
        ]
    )

    assert exit_code == 0
    assert "may repeat external side effects" in capsys.readouterr().err


def _copy_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        Path("tests/fixtures/minimal.config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return config_path


def _message(message_id: str, sent_at: str) -> dict:
    return {
        "message_id": message_id,
        "chat_id": "oc_test",
        "chat_type": "group",
        "sender_id": "ou_user",
        "sender_name": "User",
        "create_time": sent_at,
        "text": "@Owner help",
        "mentions": [{"open_id": "ou_owner"}],
    }


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
