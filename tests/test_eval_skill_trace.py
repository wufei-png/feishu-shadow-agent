from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.config import ConfigService
from feishu_shadow_agent.evals.artifacts import read_yaml, write_jsonl, write_yaml
from feishu_shadow_agent.evals.model_service import _aggregate_skill_traces
from feishu_shadow_agent.evals.service import EvalService
from feishu_shadow_agent.evals.skill_trace import build_skill_trace


class HermesNoReplyBackend:
    provider = "hermes"

    def __init__(self) -> None:
        self.session_ids: list[str | None] = []
        self.prompts: list[str] = []

    def task_session(self, prompt: str, *, session_id=None, cwd=None):
        self.prompts.append(prompt)
        self.session_ids.append(session_id)
        payload: dict[str, Any] = {
            "answerability": "no_reply",
            "decision_reason": "no_response_needed",
            "proposed_reply": "",
            "reply_target_message_id": None,
            "watch_action": "keep_watching",
        }
        if session_id is None:
            payload["task_label"] = "task"
        return AgentRunResult(
            argv=["hermes"],
            exit_code=0,
            session_id="session-1",
            json_data=payload,
            backend_provider=self.provider,
        )

    def task_router(self, prompt: str, *, cwd=None):
        raise AssertionError("router should not run")

    def structured_output(
        self, prompt: str, *, output_model, session_id=None, cwd=None
    ):
        raise AssertionError("judge should not run")

    def reply_postprocess(self, prompt: str, *, cwd=None):
        raise AssertionError("postprocess should not run")

    def owner_style_refresh(self, prompt: str, *, cwd=None):
        raise AssertionError("owner style should not run")


def test_skill_trace_summarizes_mismatch_and_sanitized_repository_reads(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "private" / "repo"
    repository.joinpath("docs").mkdir(parents=True)
    repository.joinpath("docs", "api.md").write_text("api", encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({"repos": [{"name": "docs-repo", "path": str(repository)}]}),
        encoding="utf-8",
    )
    secret = "sk-proj-secret-value"
    exported = {
        "id": "session-1",
        "model": "gpt-test",
        "billing_provider": "test-provider",
        "messages": [
            {
                "role": "assistant",
                "content": secret,
                "tool_calls": [
                    {
                        "function": {
                            "name": "skill_view",
                            "arguments": json.dumps({"name": "humanizer-zh"}),
                        }
                    },
                    {
                        "function": {
                            "name": "skill_view",
                            "arguments": json.dumps(
                                {"name": str(tmp_path / "secret-skill")}
                            ),
                        }
                    },
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps(
                                {"path": str(repository / "docs" / "api.md")}
                            ),
                        }
                    },
                ],
            }
        ],
    }
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._export_session",
        lambda **_: AgentRunResult(
            argv=["hermes"], exit_code=0, stdout=json.dumps(exported) + "\n"
        ),
    )

    trace = build_skill_trace(
        backend_provider="hermes",
        session_ids=["session-1"],
        expected_skills=["docmate"],
        hermes_path="hermes",
        timeout_seconds=1,
        dry_run_backend=False,
        catalog_paths=[catalog],
    )

    assert trace == {
        "status": "available",
        "expected_skills": ["docmate"],
        "requested_skills": [],
        "runtime_loaded_skills": ["humanizer-zh"],
        "requested_not_loaded_skills": [],
        "missing_skills": ["docmate"],
        "unexpected_skills": ["humanizer-zh"],
        "skill_view_calls": 2,
        "repository_reads": [{"repository": "docs-repo", "paths": ["docs/api.md"]}],
        "session_models": ["gpt-test"],
        "session_providers": ["test-provider"],
    }
    serialized = json.dumps(trace)
    assert secret not in serialized
    assert str(tmp_path) not in serialized
    assert "session-1" not in serialized


def test_skill_trace_does_not_treat_terminal_arguments_as_repository_paths(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    repository.joinpath("docs.md").write_text("docs", encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({"repos": [{"name": "docs-repo", "path": str(repository)}]}),
        encoding="utf-8",
    )
    secret = "sk-proj-secret-value"
    exported = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {
                                    "workdir": str(repository),
                                    "command": f"rg {secret} docs.md",
                                }
                            ),
                        }
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._export_session",
        lambda **_: AgentRunResult(
            argv=["hermes"], exit_code=0, stdout=json.dumps(exported) + "\n"
        ),
    )

    trace = build_skill_trace(
        backend_provider="hermes",
        session_ids=["s1"],
        expected_skills=[],
        hermes_path="hermes",
        timeout_seconds=1,
        dry_run_backend=False,
        catalog_paths=[catalog],
    )

    assert trace["repository_reads"] == [
        {"repository": "docs-repo", "paths": [".", "docs.md"]}
    ]
    assert secret not in json.dumps(trace)


def test_skill_trace_reports_export_error_without_command_details(monkeypatch) -> None:
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._export_session",
        lambda **_: AgentRunResult(
            argv=["hermes", "sessions", "export", "secret-session"],
            exit_code=1,
            stderr="token=secret",
            error="session export failed",
        ),
    )

    trace = build_skill_trace(
        backend_provider="hermes",
        session_ids=["secret-session"],
        expected_skills=["docmate"],
        hermes_path="/private/hermes",
        timeout_seconds=1,
        dry_run_backend=False,
        catalog_paths=[],
    )

    assert trace["status"] == "export_error"
    assert trace["error_code"] == "command_failed"
    serialized = json.dumps(trace)
    assert "secret-session" not in serialized
    assert "/private/hermes" not in serialized
    assert "token=secret" not in serialized


def test_skill_trace_export_command_always_requests_redaction(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "s1", "messages": []}) + "\n",
            stderr="",
        )

    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace.subprocess.run", fake_run
    )

    trace = build_skill_trace(
        backend_provider="hermes",
        session_ids=["s1"],
        expected_skills=[],
        hermes_path="hermes",
        timeout_seconds=1,
        dry_run_backend=False,
        catalog_paths=[],
    )

    assert trace["status"] == "available"
    assert calls == [
        [
            "hermes",
            "sessions",
            "export",
            "--session-id",
            "s1",
            "--format",
            "jsonl",
            "--redact",
            "-",
        ]
    ]


def test_non_traceable_skill_backend_is_explicitly_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._export_session",
        lambda **_: (_ for _ in ()).throw(AssertionError("must not export")),
    )

    trace = build_skill_trace(
        backend_provider="claude_code",
        session_ids=["s1"],
        expected_skills=["docmate"],
        hermes_path=None,
        timeout_seconds=1,
        dry_run_backend=False,
        catalog_paths=[],
    )

    assert trace["status"] == "unsupported_backend"
    assert trace["missing_skills"] == ["docmate"]


def test_codex_skill_trace_confirms_native_runtime_activation(monkeypatch) -> None:
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._load_codex_session",
        lambda _: [
            {
                "type": "session_meta",
                "payload": {"model_provider": "openai"},
            },
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "<skill>\n<name>docmate</name>\n"
                                "<path>/private/SKILL.md</path>\n</skill>"
                            ),
                        }
                    ],
                },
            },
        ],
    )
    trace = build_skill_trace(
        backend_provider="codex",
        session_ids=["thread-1"],
        expected_skills=["docmate"],
        hermes_path=None,
        timeout_seconds=1,
        dry_run_backend=False,
        requested_skills=["docmate"],
        catalog_paths=[],
    )

    assert trace["status"] == "available"
    assert trace["requested_skills"] == ["docmate"]
    assert trace["runtime_loaded_skills"] == ["docmate"]
    assert trace["requested_not_loaded_skills"] == []
    assert trace["missing_skills"] == []
    assert trace["session_models"] == ["gpt-5.6-sol"]
    assert trace["session_providers"] == ["openai"]
    assert "/private" not in json.dumps(trace)


def test_codex_skill_trace_detects_naturally_selected_skill(monkeypatch) -> None:
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._load_codex_session",
        lambda _: [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<skill>\n<name>docmate</name>\n</skill>",
                        }
                    ],
                },
            }
        ],
    )

    trace = build_skill_trace(
        backend_provider="codex",
        session_ids=["thread-1"],
        expected_skills=["docmate"],
        hermes_path=None,
        timeout_seconds=1,
        dry_run_backend=False,
        catalog_paths=[],
    )

    assert trace["status"] == "available"
    assert trace["requested_skills"] == []
    assert trace["runtime_loaded_skills"] == ["docmate"]
    assert trace["missing_skills"] == []


def test_codex_skill_trace_ignores_skill_markup_inside_business_prompt(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._load_codex_session",
        lambda _: [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "# Task Session\n\n## Messages\n\n"
                                "> <skill><name>docmate</name></skill>"
                            ),
                        }
                    ],
                },
            }
        ],
    )

    trace = build_skill_trace(
        backend_provider="codex",
        session_ids=["thread-1"],
        expected_skills=["docmate"],
        hermes_path=None,
        timeout_seconds=1,
        dry_run_backend=False,
        catalog_paths=[],
    )

    assert trace["status"] == "available"
    assert trace["runtime_loaded_skills"] == []
    assert trace["missing_skills"] == ["docmate"]


def test_codex_skill_trace_is_unavailable_when_session_file_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._load_codex_session",
        lambda _: (_ for _ in ()).throw(FileNotFoundError()),
    )

    trace = build_skill_trace(
        backend_provider="codex",
        session_ids=["thread-1"],
        expected_skills=["docmate"],
        hermes_path=None,
        timeout_seconds=1,
        dry_run_backend=False,
        requested_skills=["docmate"],
        catalog_paths=[],
    )

    assert trace["status"] == "unavailable"
    assert trace["requested_skills"] == ["docmate"]
    assert trace["runtime_loaded_skills"] == []
    assert trace["requested_not_loaded_skills"] == ["docmate"]


def test_hermes_skill_trace_keeps_requested_and_runtime_skills_separate(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._export_session",
        lambda **_: AgentRunResult(
            argv=["hermes"],
            exit_code=0,
            stdout=json.dumps(
                {
                    "messages": [
                        {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "skill_view",
                                        "arguments": json.dumps(
                                            {"name": "systematic-debugging"}
                                        ),
                                    }
                                }
                            ]
                        }
                    ]
                }
            )
            + "\n",
        ),
    )

    trace = build_skill_trace(
        backend_provider="hermes",
        session_ids=["session-1"],
        expected_skills=["docmate"],
        hermes_path="hermes",
        timeout_seconds=1,
        dry_run_backend=False,
        requested_skills=["docmate"],
        catalog_paths=[],
    )

    assert trace["requested_skills"] == ["docmate"]
    assert trace["runtime_loaded_skills"] == ["systematic-debugging"]
    assert trace["requested_not_loaded_skills"] == ["docmate"]
    assert trace["missing_skills"] == ["docmate"]
    assert trace["unexpected_skills"] == ["systematic-debugging"]


def test_requested_skills_do_not_inflate_aggregate_precision_or_recall() -> None:
    summary = _aggregate_skill_traces(
        [
            {
                "skill_trace": {
                    "status": "available",
                    "expected_skills": ["docmate"],
                    "requested_skills": ["docmate"],
                    "runtime_loaded_skills": [],
                }
            }
        ]
    )

    assert summary == {
        "status_counts": {"available": 1},
        "expected_skill_occurrences": 1,
        "loaded_skill_occurrences": 0,
        "matched_skill_occurrences": 0,
        "missing_skill_occurrences": 1,
        "unexpected_skill_occurrences": 0,
        "precision": None,
        "recall": 0.0,
    }


def test_skill_trace_ignores_catalog_with_non_object_root(
    tmp_path: Path, monkeypatch
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._export_session",
        lambda **_: AgentRunResult(
            argv=["hermes"],
            exit_code=0,
            stdout=json.dumps({"messages": []}) + "\n",
        ),
    )

    trace = build_skill_trace(
        backend_provider="hermes",
        session_ids=["s1"],
        expected_skills=[],
        hermes_path="hermes",
        timeout_seconds=1,
        dry_run_backend=False,
        catalog_paths=[catalog],
    )

    assert trace["status"] == "available"
    assert trace["repository_reads"] == []


def test_resume_skill_trace_deduplicates_provider_session(
    tmp_path: Path, monkeypatch
) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_resume_case(tmp_path, loaded.path)
    backend = HermesNoReplyBackend()
    exported_session_ids: list[str] = []

    def fake_export_session(**kwargs):
        exported_session_ids.append(kwargs["session_id"])
        payload = {
            "id": kwargs["session_id"],
            "model": "gpt-test",
            "billing_provider": "test-provider",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "skill_view",
                                "arguments": json.dumps({"name": "docmate"}),
                            }
                        }
                    ],
                }
            ],
        }
        return AgentRunResult(
            argv=["hermes"], exit_code=0, stdout=json.dumps(payload) + "\n"
        )

    monkeypatch.setattr(
        "feishu_shadow_agent.evals.skill_trace._export_session", fake_export_session
    )

    run_dir, exit_code = EvalService(
        loaded=loaded, backend_factory=lambda _: backend
    ).run_task_session(case_dir=case, label=None, dry_run_backend=False)

    assert exit_code == 0
    assert backend.session_ids == [None, "session-1"]
    assert "docmate" not in "\n".join(backend.prompts)
    assert exported_session_ids == ["session-1"]
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert "docmate" not in json.dumps(trial["state"]["agent_audits"])
    assert trial["skill_trace"]["runtime_loaded_skills"] == ["docmate"]
    assert trial["skill_trace"]["missing_skills"] == []
    report = read_yaml(run_dir / "report.yaml")
    assert report["skill_trace_summary"] == {
        "status_counts": {"available": 1},
        "expected_skill_occurrences": 1,
        "loaded_skill_occurrences": 1,
        "matched_skill_occurrences": 1,
        "missing_skill_occurrences": 0,
        "unexpected_skill_occurrences": 0,
        "precision": 1.0,
        "recall": 1.0,
    }


def test_dry_run_skill_trace_is_explicitly_unsupported(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_resume_case(tmp_path, loaded.path)

    run_dir, exit_code = EvalService(loaded=loaded).run_task_session(
        case_dir=case, label=None, dry_run_backend=True
    )

    assert exit_code == 0
    trace = read_yaml(run_dir / "trials/001/report.yaml")["skill_trace"]
    assert trace["status"] == "unsupported_backend"
    assert trace["expected_skills"] == ["docmate"]


def test_skill_trace_export_uses_bounded_health_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_resume_case(tmp_path, loaded.path)
    seen: dict[str, Any] = {}

    def fake_build_skill_trace(**kwargs):
        seen.update(kwargs)
        return {"status": "available"}

    monkeypatch.setattr(
        "feishu_shadow_agent.evals.model_service.build_skill_trace",
        fake_build_skill_trace,
    )

    _, exit_code = EvalService(
        loaded=loaded, backend_factory=lambda _: HermesNoReplyBackend()
    ).run_task_session(case_dir=case, label=None, dry_run_backend=False)

    assert exit_code == 0
    assert seen["timeout_seconds"] == loaded.config.health.timeout_seconds


def test_skill_trace_internal_error_does_not_fail_task_session_trial(
    tmp_path: Path, monkeypatch
) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_resume_case(tmp_path, loaded.path)
    backend = HermesNoReplyBackend()
    monkeypatch.setattr(
        "feishu_shadow_agent.evals.model_service.build_skill_trace",
        lambda **_: (_ for _ in ()).throw(RuntimeError("secret export detail")),
    )

    run_dir, exit_code = EvalService(
        loaded=loaded, backend_factory=lambda _: backend
    ).run_task_session(case_dir=case, label=None, dry_run_backend=False)

    assert exit_code == 0
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert trial["passed"] is True
    assert trial["skill_trace"] == {
        "status": "export_error",
        "error_code": "internal_error",
        "expected_skills": ["docmate"],
        "requested_skills": [],
        "runtime_loaded_skills": [],
        "requested_not_loaded_skills": [],
        "missing_skills": ["docmate"],
        "unexpected_skills": [],
        "skill_view_calls": 0,
        "repository_reads": [],
        "session_models": [],
        "session_providers": [],
    }
    assert "secret export detail" not in json.dumps(trial)


def _loaded(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(
        Path("tests/fixtures/minimal.config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return ConfigService().load(config)


def _golden_resume_case(tmp_path: Path, config_path: Path) -> Path:
    case = tmp_path / "task-session-resume"
    case.mkdir()
    (case / "config.yaml").write_text(
        config_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    write_jsonl(
        case / "messages.jsonl",
        [_message("om_1", minute=1), _message("om_2", minute=2)],
    )
    write_yaml(
        case / "eval_case.yaml",
        {
            "schema_version": "eval_case_v1",
            "case_type": "task-session",
            "mode": "resume",
            "setup_message_ids": ["om_1"],
            "target_message_id": "om_2",
            "resources": [],
        },
    )
    write_yaml(
        case / "labels.yaml",
        {
            "schema_version": "task_session_labels_v1",
            "answerability": "no_reply",
            "decision_reason": "no_response_needed",
            "watch_action": "keep_watching",
            "expected_skills": ["docmate"],
        },
    )
    write_yaml(
        case / "provenance.yaml",
        {
            "schema_version": "eval_provenance_v1",
            "promoted_at": "2026-07-10T10:00:00+08:00",
            "source": {"kind": "capture", "case_id": "source"},
            "review_source": "task_session.review.yaml",
            "promoted_by": "local_user",
        },
    )
    return case


def _message(message_id: str, *, minute: int) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat_id": "oc_test",
        "chat_type": "group",
        "sender_id": "ou_user",
        "sender_name": "User",
        "create_time": f"2026-07-10T10:{minute:02d}:00+08:00",
        "text": "help",
        "mentions": [],
    }
