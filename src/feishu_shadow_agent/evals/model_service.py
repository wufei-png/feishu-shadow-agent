from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agent_backend import AgentBackend
from ..config import ConfigService, LoadedConfig
from .artifacts import (
    EvalError,
    copy_config_or_raise,
    evals_base_dir,
    file_sha256,
    model_metadata,
    read_yaml,
    reserve_run_dir,
    text_sha256,
    validate_config_copy,
    write_metadata,
    write_yaml,
)
from .backend_trace import TracedAgentBackend, merge_prompt_hashes
from .cases import LoadedEvalCase, load_eval_case, message_sent_at, scenario_message_ids
from .dry_run import DryRunBackend
from .full_chain_eval import run_full_chain_trial
from .router_eval import run_router_trial
from .runtime import TrialRuntime
from .task_session_eval import run_task_session_trial

BackendFactory = Callable[[LoadedConfig], AgentBackend]


class ModelEvalService:
    def __init__(
        self,
        *,
        loaded: LoadedConfig,
        backend_factory: BackendFactory,
    ):
        self.loaded = loaded
        self.backend_factory = backend_factory
        self.base_dir = evals_base_dir(loaded)

    def run_case(
        self,
        *,
        eval_type: str,
        case_dir: Path,
        label: str | None,
        dry_run_backend: bool,
        repeat: int,
        allow_sensitive_config: bool,
    ) -> tuple[Path, int]:
        _validate_repeat(repeat)
        validate_config_copy(
            loaded=self.loaded,
            allow_sensitive_config=allow_sensitive_config,
        )
        _, run_dir = reserve_run_dir(
            self.base_dir / "runs" / eval_type,
            eval_type,
            label,
        )
        try:
            report, exit_code = self._run_case_into(
                eval_type=eval_type,
                case_dir=case_dir,
                run_dir=run_dir,
                dry_run_backend=dry_run_backend,
                repeat=repeat,
                allow_sensitive_config=allow_sensitive_config,
            )
        except Exception as exc:  # noqa: BLE001
            exit_code = 2
            report = {
                "schema_version": "eval_case_report_v1",
                "eval_type": eval_type,
                "case": str(case_dir),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "run_config_hash": file_sha256(self.loaded.path),
                "passed": False,
            }
        write_yaml(run_dir / "report.yaml", report)
        return run_dir, exit_code

    def run_cases(
        self,
        *,
        eval_type: str,
        cases_dir: Path,
        label: str | None,
        dry_run_backend: bool,
        repeat: int,
        allow_sensitive_config: bool,
    ) -> tuple[Path, int]:
        _validate_repeat(repeat)
        validate_config_copy(
            loaded=self.loaded,
            allow_sensitive_config=allow_sensitive_config,
        )
        case_dirs = _matching_case_dirs(cases_dir, eval_type)
        _, run_dir = reserve_run_dir(
            self.base_dir / "runs" / eval_type,
            f"{eval_type}-batch",
            label,
        )
        config_info = copy_config_or_raise(
            loaded=self.loaded,
            destination_dir=run_dir,
            allow_sensitive_config=allow_sensitive_config,
        )
        write_metadata(
            run_dir,
            loaded=self.loaded,
            config_info=config_info,
        )
        results: list[dict[str, Any]] = []
        case_prompt_hashes: list[dict[str, str]] = []
        exit_code = 0
        for index, case_dir in enumerate(case_dirs, start=1):
            case_output = run_dir / "cases" / f"{index:03d}-{_safe_name(case_dir.name)}"
            case_output.mkdir(parents=True, exist_ok=False)
            try:
                report, case_exit = self._run_case_into(
                    eval_type=eval_type,
                    case_dir=case_dir,
                    run_dir=case_output,
                    dry_run_backend=dry_run_backend,
                    repeat=repeat,
                    copy_run_config=False,
                    allow_sensitive_config=allow_sensitive_config,
                )
            except Exception as exc:  # noqa: BLE001
                case_exit = 2
                report = {
                    "schema_version": "eval_case_report_v1",
                    "eval_type": eval_type,
                    "case": str(case_dir),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "run_config_hash": file_sha256(self.loaded.path),
                    "passed": False,
                }
            write_yaml(case_output / "report.yaml", report)
            if isinstance(report.get("prompt_hashes"), dict):
                case_prompt_hashes.append(report["prompt_hashes"])
            results.append(
                {
                    "case": str(case_dir),
                    "report": str(case_output / "report.yaml"),
                    "exit_code": case_exit,
                    "passed": report.get("passed"),
                }
            )
            if case_exit == 2:
                exit_code = 2
            elif case_exit == 1 and exit_code == 0:
                exit_code = 1
        summary = {
            "schema_version": "eval_batch_summary_v1",
            "eval_type": eval_type,
            "repeat": repeat,
            "case_count": len(case_dirs),
            "results": results,
            "passed": _batch_passed(results=results, exit_code=exit_code),
        }
        write_yaml(run_dir / "summary.yaml", summary)
        _update_metadata_prompt_hashes(run_dir, merge_prompt_hashes(case_prompt_hashes))
        return run_dir, exit_code

    def _run_case_into(
        self,
        *,
        eval_type: str,
        case_dir: Path,
        run_dir: Path,
        dry_run_backend: bool,
        repeat: int,
        copy_run_config: bool = True,
        allow_sensitive_config: bool = False,
    ) -> tuple[dict[str, Any], int]:
        if copy_run_config:
            config_info = copy_config_or_raise(
                loaded=self.loaded,
                destination_dir=run_dir,
                allow_sensitive_config=allow_sensitive_config,
            )
            write_metadata(
                run_dir,
                loaded=self.loaded,
                config_info=config_info,
            )
        case = load_eval_case(
            case_dir=case_dir,
            case_type=eval_type,
            run_config=self.loaded,
        )
        run_config_hash = file_sha256(self.loaded.path)
        case_backend = model_metadata(
            ConfigService().load(case.directory / "config.yaml")
        )
        run_backend = model_metadata(self.loaded)
        trial_reports: list[dict[str, Any]] = []
        for trial_number in range(1, repeat + 1):
            evidence_dir = run_dir / "trials" / f"{trial_number:03d}"
            runtime: TrialRuntime | None = None
            traced_backend: TracedAgentBackend | None = None
            try:
                runtime = TrialRuntime.create(
                    loaded=self.loaded,
                    evidence_dir=evidence_dir,
                    initial_time=_initial_time(case),
                    slot_key=text_sha256(str(case.directory))[:24],
                    log_level=self.loaded.config.logging.level,
                )
                backend = (
                    DryRunBackend()
                    if dry_run_backend
                    else self.backend_factory(self.loaded)
                )
                traced_backend = TracedAgentBackend(backend)
                trial = _run_trial(
                    eval_type=eval_type,
                    case=case,
                    runtime=runtime,
                    loaded=self.loaded,
                    backend=traced_backend,
                    run_id=f"trial-{trial_number:03d}",
                )
                trial["trial"] = trial_number
                trial["status"] = (
                    "unscored"
                    if trial.get("passed") is None
                    else "passed"
                    if trial.get("passed")
                    else "failed"
                )
            except Exception as exc:  # noqa: BLE001
                trial = {
                    "schema_version": f"{eval_type.replace('-', '_')}_trial_report_v1",
                    "trial": trial_number,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "state": None if runtime is None else runtime.state_summary(),
                    "passed": False,
                }
            finally:
                if traced_backend is not None:
                    trial["prompt_hashes"] = traced_backend.prompt_hashes()
                    if self.loaded.config.debug.save_full_agent_io:
                        traced_backend.write_prompts(evidence_dir / "prompts")
                if runtime is not None:
                    try:
                        runtime.close()
                    except Exception as exc:  # noqa: BLE001
                        trial["status"] = "error"
                        trial["passed"] = False
                        trial["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            write_yaml(evidence_dir / "report.yaml", trial)
            trial_reports.append(trial)
        aggregate = _aggregate_trials(trial_reports, label_status=case.status)
        prompt_hashes = merge_prompt_hashes(
            [
                row["prompt_hashes"]
                for row in trial_reports
                if isinstance(row.get("prompt_hashes"), dict)
            ]
        )
        report = {
            "schema_version": "eval_case_report_v1",
            "eval_type": eval_type,
            "case": str(case.directory),
            "label_status": case.status,
            "repeat": repeat,
            "dry_run_backend": dry_run_backend,
            "tool_permissions": self.loaded.config.tool_permissions,
            "case_config_hash": case.case_config_hash,
            "run_config_hash": run_config_hash,
            "config_changed": case.case_config_hash != run_config_hash,
            "case_agent_backend": case_backend,
            "run_agent_backend": run_backend,
            "backend_changed": case_backend != run_backend,
            "prompt_hashes": prompt_hashes,
            "trials": [
                {
                    "trial": row["trial"],
                    "status": row["status"],
                    "report": f"trials/{row['trial']:03d}/report.yaml",
                }
                for row in trial_reports
            ],
            **aggregate,
        }
        if copy_run_config:
            _update_metadata_prompt_hashes(run_dir, prompt_hashes)
        return report, _exit_code(report)


def _run_trial(
    *,
    eval_type: str,
    case: LoadedEvalCase,
    runtime: TrialRuntime,
    loaded: LoadedConfig,
    backend: AgentBackend,
    run_id: str,
) -> dict[str, Any]:
    if eval_type == "router":
        return run_router_trial(
            case=case,
            runtime=runtime,
            loaded=loaded,
            backend=backend,
            run_id=run_id,
        )
    if eval_type == "task-session":
        return run_task_session_trial(
            case=case,
            runtime=runtime,
            loaded=loaded,
            backend=backend,
            run_id=run_id,
        )
    if eval_type == "full-chain":
        return run_full_chain_trial(
            case=case,
            runtime=runtime,
            loaded=loaded,
            backend=backend,
            run_id=run_id,
        )
    raise EvalError(f"unsupported model eval type: {eval_type}")


def _aggregate_trials(
    trials: list[dict[str, Any]], *, label_status: str
) -> dict[str, Any]:
    passed = sum(row["status"] == "passed" for row in trials)
    failed = sum(row["status"] == "failed" for row in trials)
    errors = sum(row["status"] == "error" for row in trials)
    unscored = sum(row["status"] == "unscored" for row in trials)
    differences: dict[str, int] = {}
    for trial in trials:
        semantic = trial.get("semantic")
        if not isinstance(semantic, dict):
            continue
        for difference in semantic.get("differences") or []:
            if not isinstance(difference, dict):
                continue
            difference_type = str(difference.get("type") or "")
            if difference_type:
                differences[difference_type] = differences.get(difference_type, 0) + 1
    case_passed: bool | None
    if errors:
        case_passed = False
    elif label_status != "golden":
        case_passed = None
    else:
        case_passed = passed == len(trials)
    return {
        "passed_trials": passed,
        "failed_trials": failed,
        "error_trials": errors,
        "unscored_trials": unscored,
        "pass_rate": None if not trials else round(passed / len(trials), 4),
        "semantic_difference_counts": differences,
        "passed": case_passed,
    }


def _exit_code(report: dict[str, Any]) -> int:
    if report["error_trials"]:
        return 2
    if report["label_status"] == "golden" and report["passed"] is False:
        return 1
    return 0


def _batch_passed(*, results: list[dict[str, Any]], exit_code: int) -> bool | None:
    if exit_code != 0:
        return False
    if all(row.get("passed") is True for row in results):
        return True
    return None


def _initial_time(case: LoadedEvalCase) -> str:
    values = [
        message_sent_at(case.raw_messages[message_id])
        for message_id in scenario_message_ids(case.scenario)
    ]
    return min(values, key=datetime.fromisoformat)


def _matching_case_dirs(cases_dir: Path, eval_type: str) -> list[Path]:
    root = cases_dir.expanduser().resolve()
    if not root.is_dir():
        raise EvalError(f"cases directory does not exist: {root}")
    review_name = f"{eval_type.replace('-', '_')}.review.yaml"
    matches: list[Path] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        if (path / review_name).is_file():
            matches.append(path)
            continue
        manifest = path / "eval_case.yaml"
        if not manifest.is_file():
            continue
        from .artifacts import read_yaml

        try:
            manifest_data = read_yaml(manifest)
        except EvalError:
            matches.append(path)
            continue
        if manifest_data.get("case_type") == eval_type:
            matches.append(path)
    if not matches:
        raise EvalError(f"no {eval_type} eval cases found under {root}")
    return matches


def _validate_repeat(repeat: int) -> None:
    if repeat < 1:
        raise EvalError("--repeat must be at least 1")


def _safe_name(value: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in value
    )
    return cleaned[:100] or "case"


def _update_metadata_prompt_hashes(
    run_dir: Path, prompt_hashes: dict[str, str]
) -> None:
    path = run_dir / "metadata.yaml"
    if not path.is_file():
        return
    metadata = read_yaml(path)
    metadata["prompt_hashes"] = prompt_hashes
    write_yaml(path, metadata)
