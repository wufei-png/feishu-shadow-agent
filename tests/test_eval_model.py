from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.config import ConfigService
from feishu_shadow_agent.evals.artifacts import read_yaml, write_jsonl, write_yaml
from feishu_shadow_agent.evals.cases import resource_fixture_path
from feishu_shadow_agent.evals.dry_run import DryRunBackend
from feishu_shadow_agent.evals.schemas import ResourceFixture
from feishu_shadow_agent.evals.service import EvalService


class NoSessionBackend:
    provider = "test"

    def task_session(self, prompt: str, *, session_id=None, cwd=None):
        return AgentRunResult(
            argv=["test"],
            exit_code=0,
            session_id=None,
            json_data={
                "answerability": "no_reply",
                "decision_reason": "no_response_needed",
                "proposed_reply": "",
                "reply_target_message_id": None,
                "watch_action": "keep_watching",
                "task_label": "setup",
            },
            backend_provider=self.provider,
        )

    def task_router(self, prompt: str, *, cwd=None):
        raise AssertionError("router should not run")

    def structured_output(
        self,
        prompt: str,
        *,
        output_model: type[BaseModel],
        session_id=None,
        cwd=None,
    ):
        raise AssertionError("judge should not run")

    def reply_postprocess(self, prompt: str, *, cwd=None):
        raise AssertionError("postprocess should not run")

    def owner_style_refresh(self, prompt: str, *, cwd=None):
        raise AssertionError("owner style should not run")


class StatefulNoReplyBackend(NoSessionBackend):
    def __init__(self) -> None:
        self.session_ids: list[str | None] = []

    def task_session(self, prompt: str, *, session_id=None, cwd=None):
        self.session_ids.append(session_id)
        payload: dict[str, Any] = {
            "answerability": "no_reply",
            "decision_reason": "no_response_needed",
            "proposed_reply": "",
            "reply_target_message_id": None,
            "watch_action": "keep_watching",
        }
        if session_id is None:
            payload["task_label"] = "captured task"
        return AgentRunResult(
            argv=["test"],
            exit_code=0,
            session_id=session_id or "session-1",
            json_data=payload,
            backend_provider=self.provider,
        )


class AutoReplyBackend(StatefulNoReplyBackend):
    def task_session(self, prompt: str, *, session_id=None, cwd=None):
        self.session_ids.append(session_id)
        payload: dict[str, Any] = {
            "answerability": "auto_reply",
            "decision_reason": None,
            "proposed_reply": "事实 A",
            "reply_target_message_id": "om_1",
            "watch_action": "keep_watching",
        }
        if session_id is None:
            payload["task_label"] = "auto reply task"
        return AgentRunResult(
            argv=["test"],
            exit_code=0,
            session_id=session_id or "session-1",
            json_data=payload,
            backend_provider=self.provider,
        )

    def structured_output(
        self,
        prompt: str,
        *,
        output_model: type[BaseModel],
        session_id=None,
        cwd=None,
    ):
        return AgentRunResult(
            argv=["judge"],
            exit_code=0,
            json_data={"verdict": "pass", "differences": []},
            backend_provider=self.provider,
        )


class MismatchedDecisionBackend(StatefulNoReplyBackend):
    def task_session(self, prompt: str, *, session_id=None, cwd=None):
        self.session_ids.append(session_id)
        return AgentRunResult(
            argv=["test"],
            exit_code=0,
            session_id=session_id or "session-1",
            json_data={
                "answerability": "auto_reply",
                "decision_reason": "sufficient_evidence_low_risk",
                "proposed_reply": (
                    "已只读核对当前 Deployment 已包含建议参数，新 Pod 连续运行且无重启，"
                    "历史修复已经实施，无需再次申请修改。"
                ),
                "reply_target_message_id": "om_1",
                "watch_action": "close",
                "task_label": "核对恢复状态",
            },
            backend_provider=self.provider,
        )


def test_dry_run_followup_does_not_infer_schema_from_untrusted_prompt_text() -> None:
    result = DryRunBackend().task_session(
        "quoted user text: - `task_label`: injected", session_id="session-1"
    )

    assert result.ok
    assert "task_label" not in result.json_data


def test_router_repeat_rebuilds_identical_db_state(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "router",
        [_message("om_1", minute=1)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "router",
            "target": {"message_id": "om_1", "source": "group_at_me"},
            "tasks": {},
        },
        {"schema_version": "router_labels_v1", "route": "new_task"},
    )

    run_dir, exit_code = EvalService(loaded=loaded).run_router(
        case_dir=case,
        label="repeat",
        dry_run_backend=True,
        repeat=2,
    )

    assert exit_code == 0
    first = read_yaml(run_dir / "trials/001/report.yaml")
    second = read_yaml(run_dir / "trials/002/report.yaml")
    assert first["state"] == second["state"]
    assert not list(run_dir.rglob("*.sqlite3"))
    report = read_yaml(run_dir / "report.yaml")
    assert report["passed_trials"] == 2
    assert report["pass_rate"] == 1.0
    assert report["backend_changed"] is False


def test_router_scores_stable_task_alias(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    first = _message("om_1", minute=1)
    target = _message("om_2", minute=2, reply_to="om_1")
    case = _golden_case(
        tmp_path,
        loaded.path,
        "router",
        [first, target],
        {
            "schema_version": "eval_case_v1",
            "case_type": "router",
            "target": {"message_id": "om_2", "source": "group_at_me"},
            "tasks": {
                "task_1": {
                    "status": "watching",
                    "task_label": "first",
                    "message_ids": ["om_1"],
                }
            },
        },
        {
            "schema_version": "router_labels_v1",
            "route": "attach_task",
            "task_key": "task_1",
        },
    )

    run_dir, exit_code = EvalService(loaded=loaded).run_router(
        case_dir=case, label=None, dry_run_backend=True
    )

    assert exit_code == 0
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert trial["actual"]["task_key"] == "task_1"


def test_router_runs_model_only_for_placeholder_path(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    first = _message("om_1", minute=1)
    second = _message("om_2", minute=2)
    target = _message("om_3", minute=5)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "router-placeholder",
        [first, second, target],
        {
            "schema_version": "eval_case_v1",
            "case_type": "router",
            "target": {"message_id": "om_3", "source": "group_at_me"},
            "tasks": {
                "task_1": {
                    "status": "watching",
                    "task_label": "first",
                    "message_ids": ["om_1"],
                },
                "task_2": {
                    "status": "watching",
                    "task_label": "second",
                    "message_ids": ["om_2"],
                },
            },
        },
        {"schema_version": "router_labels_v1", "route": "ambiguous"},
    )

    run_dir, exit_code = EvalService(loaded=loaded).run_router(
        case_dir=case, label=None, dry_run_backend=True
    )

    assert exit_code == 0
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert trial["actual"]["route"] == "ambiguous"
    assert trial["actual"]["router_called"] is True


def test_task_session_repeat_rebuilds_backend_session_and_db_state(
    tmp_path: Path,
) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "task-session-repeat",
        [_message("om_1", minute=1)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "task-session",
            "mode": "initial",
            "message_ids": ["om_1"],
            "resources": [],
        },
        {
            "schema_version": "task_session_labels_v1",
            "answerability": "no_reply",
            "watch_action": "keep_watching",
        },
    )
    backends: list[StatefulNoReplyBackend] = []

    def backend_factory(_):
        backend = StatefulNoReplyBackend()
        backends.append(backend)
        return backend

    service = EvalService(loaded=loaded, backend_factory=backend_factory)

    run_dir, exit_code = service.run_task_session(
        case_dir=case,
        label="repeat",
        dry_run_backend=False,
        repeat=2,
    )

    assert exit_code == 0
    assert [backend.session_ids for backend in backends] == [[None], [None]]
    first = read_yaml(run_dir / "trials/001/report.yaml")
    second = read_yaml(run_dir / "trials/002/report.yaml")
    assert first["state"] == second["state"]
    assert not list(run_dir.rglob("*.sqlite3"))


def test_task_session_decision_mismatch_fails_closed_without_semantic_judge(
    tmp_path: Path,
) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "task-session-later-state",
        [_message("om_1", minute=1)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "task-session",
            "mode": "initial",
            "message_ids": ["om_1"],
            "resources": [],
        },
        {
            "schema_version": "task_session_labels_v1",
            "answerability": "needs_owner",
            "watch_action": "keep_watching",
            "reference_answer": "建议增加参数，请授权修改并重启观察。",
        },
    )

    run_dir, exit_code = EvalService(
        loaded=loaded, backend_factory=lambda _: MismatchedDecisionBackend()
    ).run_task_session(case_dir=case, label=None, dry_run_backend=False)

    assert exit_code == 1
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert trial["structure"]["passed"] is False
    assert "compatibility" not in trial["structure"]
    assert trial["semantic"] == {"status": "not_scored"}
    assert trial["passed"] is False


def test_resume_setup_without_session_is_runtime_error(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "task-session",
        [_message("om_1", minute=1), _message("om_2", minute=2)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "task-session",
            "mode": "resume",
            "setup_message_ids": ["om_1"],
            "target_message_id": "om_2",
            "resources": [],
        },
        {
            "schema_version": "task_session_labels_v1",
            "answerability": "no_reply",
            "watch_action": "keep_watching",
        },
    )
    service = EvalService(loaded=loaded, backend_factory=lambda _: NoSessionBackend())

    run_dir, exit_code = service.run_task_session(
        case_dir=case, label=None, dry_run_backend=False
    )

    assert exit_code == 2
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert trial["status"] == "error"
    assert "did not return a provider session id" in trial["error"]


def test_resume_runs_real_setup_then_target_only_prompt(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "task-session-resume",
        [_message("om_1", minute=1), _message("om_2", minute=2)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "task-session",
            "mode": "resume",
            "setup_message_ids": ["om_1"],
            "target_message_id": "om_2",
            "resources": [],
        },
        {
            "schema_version": "task_session_labels_v1",
            "answerability": "no_reply",
            "watch_action": "keep_watching",
        },
    )
    backend = StatefulNoReplyBackend()

    run_dir, exit_code = EvalService(
        loaded=loaded, backend_factory=lambda _: backend
    ).run_task_session(case_dir=case, label=None, dry_run_backend=False)

    assert exit_code == 0
    assert backend.session_ids == [None, "session-1"]
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert trial["setup"]["plan"]["prompt_message_ids"] == ["om_1"]
    assert trial["target"]["plan"]["prompt_message_ids"] == ["om_2"]


def test_resume_setup_preserves_same_minute_message_position_order(
    tmp_path: Path,
) -> None:
    loaded = _loaded(tmp_path)
    first = _message("om_z", minute=1)
    first["message_position"] = "9"
    second = _message("om_a", minute=1)
    second["message_position"] = "10"
    target = _message("om_target", minute=2)
    target["message_position"] = "11"
    case = _golden_case(
        tmp_path,
        loaded.path,
        "task-session-resume-position",
        [first, second, target],
        {
            "schema_version": "eval_case_v1",
            "case_type": "task-session",
            "mode": "resume",
            "setup_message_ids": ["om_z", "om_a"],
            "target_message_id": "om_target",
            "resources": [],
        },
        {
            "schema_version": "task_session_labels_v1",
            "answerability": "no_reply",
            "watch_action": "keep_watching",
        },
    )
    backend = StatefulNoReplyBackend()

    run_dir, exit_code = EvalService(
        loaded=loaded, backend_factory=lambda _: backend
    ).run_task_session(case_dir=case, label=None, dry_run_backend=False)

    assert exit_code == 0
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert trial["setup"]["plan"]["prompt_message_ids"] == ["om_z", "om_a"]


def test_router_rejects_target_already_seeded_in_task(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "router-target-seeded",
        [_message("om_1", minute=1)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "router",
            "target": {"message_id": "om_1", "source": "group_at_me"},
            "tasks": {
                "task_1": {
                    "status": "watching",
                    "task_label": "existing",
                    "message_ids": ["om_1"],
                }
            },
        },
        {
            "schema_version": "router_labels_v1",
            "route": "attach_task",
            "task_key": "task_1",
        },
    )

    run_dir, exit_code = EvalService(loaded=loaded).run_router(
        case_dir=case, label=None, dry_run_backend=True
    )

    assert exit_code == 2
    assert (
        "target must not already belong" in read_yaml(run_dir / "report.yaml")["error"]
    )


def test_router_rejects_label_alias_missing_from_scenario(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "router-missing-alias",
        [_message("om_1", minute=1)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "router",
            "target": {"message_id": "om_1", "source": "group_at_me"},
            "tasks": {},
        },
        {
            "schema_version": "router_labels_v1",
            "route": "attach_task",
            "task_key": "task_404",
        },
    )

    run_dir, exit_code = EvalService(loaded=loaded).run_router(
        case_dir=case, label=None, dry_run_backend=True
    )

    assert exit_code == 2
    assert "task_key is not defined" in read_yaml(run_dir / "report.yaml")["error"]


def test_model_golden_rejects_wrong_provenance_source(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "router",
        [_message("om_1", minute=1)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "router",
            "target": {"message_id": "om_1", "source": "group_at_me"},
            "tasks": {},
        },
        {"schema_version": "router_labels_v1", "route": "new_task"},
    )
    provenance = read_yaml(case / "provenance.yaml")
    provenance["source"] = {"kind": "ingress_run", "run_id": "wrong"}
    write_yaml(case / "provenance.yaml", provenance)

    run_dir, exit_code = EvalService(loaded=loaded).run_router(
        case_dir=case, label=None, dry_run_backend=True
    )

    assert exit_code == 2
    assert "source must be capture" in read_yaml(run_dir / "report.yaml")["error"]


def test_full_chain_runs_setup_then_scores_target(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    setup = _message("om_1", minute=1)
    target = _message("om_2", minute=2, reply_to="om_1")
    case = _golden_case(
        tmp_path,
        loaded.path,
        "full-chain",
        [setup, target],
        {
            "schema_version": "eval_case_v1",
            "case_type": "full-chain",
            "setup": [{"message_id": "om_1", "source": "group_at_me"}],
            "target": {"message_id": "om_2", "source": "group_at_me"},
            "resources": [],
        },
        {
            "schema_version": "full_chain_labels_v1",
            "router": {
                "route": "attach_task",
                "task_key": "task_1",
            },
            "task_session": {
                "answerability": "no_reply",
                "watch_action": "keep_watching",
            },
        },
    )
    service = EvalService(
        loaded=loaded, backend_factory=lambda _: StatefulNoReplyBackend()
    )

    run_dir, exit_code = service.run_full_chain(
        case_dir=case, label=None, dry_run_backend=False
    )

    assert exit_code == 0
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert [row["message_id"] for row in trial["setup"]] == ["om_1"]
    assert trial["target"]["routing"]["route"] == "attach_task"
    assert trial["target"]["routing"]["task_key"] == "task_1"
    assert trial["target"]["router_candidates"] == [
        {
            "task_key": "task_1",
            "task_short_id": trial["state"]["tasks"][0]["short_id"],
            "status": "watching",
            "matched_by": "reply_to_msg",
        }
    ]
    assert trial["target"]["task_session_plan"] == {
        "session_resumed": True,
        "task_message_ids": ["om_1", "om_2"],
        "prompt_message_ids": ["om_2"],
        "reply_target_message_ids": ["om_2", "om_1"],
        "output_model": "FollowupTaskSessionOutput",
    }
    assert trial["passed"] is True
    assert trial["would_send"] == {"actions": [], "approvals": []}
    assert "skill_trace" not in trial
    report = read_yaml(run_dir / "report.yaml")
    assert "skill_trace_summary" not in report
    metadata = read_yaml(run_dir / "metadata.yaml")
    assert set(report["prompt_hashes"]) == {"task_session"}
    assert metadata["prompt_hashes"] == report["prompt_hashes"]


def test_full_chain_resource_uses_trial_local_fixture(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    target = {**_message("om_1", minute=1), "file_key": "file_fixture"}
    content = b"fixture bytes"
    fixture = ResourceFixture(
        message_id="om_1",
        file_key="file_fixture",
        resource_type="file",
        sha256=sha256(content).hexdigest(),
    )
    case = _golden_case(
        tmp_path,
        loaded.path,
        "full-chain-resource",
        [target],
        {
            "schema_version": "eval_case_v1",
            "case_type": "full-chain",
            "setup": [],
            "target": {"message_id": "om_1", "source": "group_at_me"},
            "resources": [fixture.model_dump(mode="json")],
        },
        {
            "schema_version": "full_chain_labels_v1",
            "router": {"route": "new_task"},
            "task_session": {
                "answerability": "no_reply",
                "watch_action": "keep_watching",
            },
        },
    )
    fixture_path = resource_fixture_path(case, fixture)
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_bytes(content)
    service = EvalService(
        loaded=loaded, backend_factory=lambda _: StatefulNoReplyBackend()
    )

    run_dir, exit_code = service.run_full_chain(
        case_dir=case, label=None, dry_run_backend=False, repeat=2
    )

    assert exit_code == 0
    first = read_yaml(run_dir / "trials/001/report.yaml")
    second = read_yaml(run_dir / "trials/002/report.yaml")
    resource = first["state"]["resources"][0]
    assert resource["download_status"] == "downloaded"
    assert resource["sha256"] == fixture.sha256
    assert ".trial-slots" in resource["path"]
    assert not Path(resource["path"]).exists()
    assert first["state"] == second["state"]
    assert first["prompt_hashes"] == second["prompt_hashes"]
    assert not list(run_dir.rglob("*.sqlite3"))


def test_full_chain_records_effective_auto_reply_and_gate_action(
    tmp_path: Path,
) -> None:
    loaded = _loaded(tmp_path)
    target = _message("om_1", minute=1)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "full-chain-auto-reply",
        [target],
        {
            "schema_version": "eval_case_v1",
            "case_type": "full-chain",
            "setup": [],
            "target": {"message_id": "om_1", "source": "group_at_me"},
            "resources": [],
        },
        {
            "schema_version": "full_chain_labels_v1",
            "router": {"route": "new_task"},
            "task_session": {
                "answerability": "auto_reply",
                "watch_action": "keep_watching",
            },
            "reference_answer": "事实 A",
        },
    )
    backend = AutoReplyBackend()

    run_dir, exit_code = EvalService(
        loaded=loaded, backend_factory=lambda _: backend
    ).run_full_chain(case_dir=case, label=None, dry_run_backend=False)

    assert exit_code == 0
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert trial["target"]["raw_reply"] == "事实 A"
    assert trial["target"]["effective_reply"].endswith("事实 A")
    assert trial["target"]["new_actions"][0]["payload"]["source"] == "auto_reply"
    assert trial["target"]["new_approvals"] == []
    assert trial["semantic"]["passed"] is True


def test_full_agent_io_is_written_only_to_prompt_bundle(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path, save_full_agent_io=True)
    case = _golden_case(
        tmp_path,
        loaded.path,
        "task-session-debug",
        [_message("om_1", minute=1)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "task-session",
            "mode": "initial",
            "message_ids": ["om_1"],
            "resources": [],
        },
        {
            "schema_version": "task_session_labels_v1",
            "answerability": "no_reply",
            "watch_action": "keep_watching",
        },
    )
    service = EvalService(
        loaded=loaded, backend_factory=lambda _: StatefulNoReplyBackend()
    )

    run_dir, exit_code = service.run_task_session(
        case_dir=case, label=None, dry_run_backend=False
    )

    assert exit_code == 0
    assert (run_dir / "trials/001/prompts/task_session.txt").is_file()
    trial = read_yaml(run_dir / "trials/001/report.yaml")
    assert "prompt" not in trial["state"]["agent_audits"][0]


def test_batch_continues_after_malformed_case_manifest(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    suite = tmp_path / "suite"
    suite.mkdir()
    _golden_case(
        suite,
        loaded.path,
        "router",
        [_message("om_1", minute=1)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "router",
            "target": {"message_id": "om_1", "source": "group_at_me"},
            "tasks": {},
        },
        {"schema_version": "router_labels_v1", "route": "new_task"},
    )
    malformed = suite / "malformed"
    malformed.mkdir()
    (malformed / "eval_case.yaml").write_text("[", encoding="utf-8")

    run_dir, exit_code = EvalService(loaded=loaded).run_router_cases(
        cases_dir=suite,
        label=None,
        dry_run_backend=True,
    )

    assert exit_code == 2
    summary = read_yaml(run_dir / "summary.yaml")
    assert summary["case_count"] == 2
    reports = [read_yaml(Path(row["report"])) for row in summary["results"]]
    assert sorted(report.get("status", "completed") for report in reports) == [
        "completed",
        "error",
    ]


def test_batch_with_unscored_case_does_not_claim_passed(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    suite = tmp_path / "suite"
    suite.mkdir()
    case = _golden_case(
        suite,
        loaded.path,
        "router-unscored",
        [_message("om_1", minute=1)],
        {
            "schema_version": "eval_case_v1",
            "case_type": "router",
            "target": {"message_id": "om_1", "source": "group_at_me"},
            "tasks": {},
        },
        {"schema_version": "router_labels_v1", "route": "new_task"},
    )
    (case / "labels.yaml").unlink()
    (case / "provenance.yaml").unlink()

    run_dir, exit_code = EvalService(loaded=loaded).run_router_cases(
        cases_dir=suite, label=None, dry_run_backend=True
    )

    assert exit_code == 0
    assert read_yaml(run_dir / "summary.yaml")["passed"] is None


def _loaded(tmp_path: Path, *, save_full_agent_io: bool = False):
    config = tmp_path / "config.yaml"
    text = Path("tests/fixtures/minimal.config.yaml").read_text(encoding="utf-8")
    config.write_text(
        text.replace(
            "save_full_agent_io: false",
            f"save_full_agent_io: {str(save_full_agent_io).lower()}",
        ),
        encoding="utf-8",
    )
    return ConfigService().load(config)


def _golden_case(
    tmp_path: Path,
    config_path: Path,
    case_type: str,
    messages: list[dict[str, Any]],
    scenario: dict[str, Any],
    labels: dict[str, Any],
) -> Path:
    case = tmp_path / f"{case_type}-case"
    case.mkdir()
    (case / "config.yaml").write_text(
        config_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    write_jsonl(case / "messages.jsonl", messages)
    write_yaml(case / "eval_case.yaml", scenario)
    write_yaml(case / "labels.yaml", labels)
    write_yaml(
        case / "provenance.yaml",
        {
            "schema_version": "eval_provenance_v1",
            "promoted_at": "2026-07-10T10:00:00+08:00",
            "source": {"kind": "capture", "case_id": "test"},
            "review_source": "test.review.yaml",
            "promoted_by": "local_user",
        },
    )
    return case


def _message(
    message_id: str, *, minute: int, reply_to: str | None = None
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": "oc_test",
        "chat_type": "group",
        "sender_id": "ou_user",
        "sender_name": "User",
        "create_time": f"2026-07-10T10:{minute:02d}:00+08:00",
        "text": "@Owner help",
        "mentions": [{"open_id": "ou_owner"}],
    }
    if reply_to:
        raw["reply_to_message_id"] = reply_to
    return raw
