from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import LoadedConfig
from ..types import utc_now_iso
from .artifacts import (
    EvalError,
    message_id_from_raw,
    read_jsonl,
    read_yaml,
    sensitive_config_paths,
    validate_label,
    write_jsonl,
    write_yaml,
)
from .cases import (
    load_eval_case,
    resource_fixture_path,
    scenario_message_ids,
)
from .ingress import validate_ingress_review_labels
from .schemas import (
    IngressScenario,
    ReviewEnvelope,
    golden_labels_model,
    scenario_model,
)


class PromotionService:
    def __init__(self, *, loaded: LoadedConfig, base_dir: Path):
        self.loaded = loaded
        self.base_dir = base_dir

    def promote(
        self,
        *,
        eval_type: str,
        run_dir: Path | None,
        case_dir: Path | None,
        review_path: Path,
        name: str,
        allow_sensitive_config: bool,
    ) -> Path:
        validate_label(name)
        if eval_type == "ingress" and case_dir is not None:
            raise EvalError("ingress promotion accepts --run, not --case")
        if eval_type != "ingress" and run_dir is not None:
            raise EvalError(f"{eval_type} promotion accepts --case, not --run")
        source = run_dir if eval_type == "ingress" else case_dir
        if source is None:
            required = "--run" if eval_type == "ingress" else "--case"
            raise EvalError(f"{required} is required for {eval_type} promotion")
        source = source.expanduser().resolve()
        if not source.is_dir():
            raise EvalError(f"promotion source does not exist: {source}")
        review = review_path.expanduser().resolve()
        if not review.is_file():
            raise EvalError(f"review file does not exist: {review}")
        _validate_source_owner(source, self.loaded)
        _validate_source_config_copy(
            source, allow_sensitive_config=allow_sensitive_config
        )
        parent = self.base_dir / "golden" / eval_type
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / name
        if target.exists():
            raise EvalError(f"golden case already exists: {target}")
        staging = parent / f".{name}.tmp-{secrets.token_hex(4)}"
        staging.mkdir(exist_ok=False)
        try:
            if eval_type == "ingress":
                self._promote_ingress(source=source, review=review, staging=staging)
            elif eval_type in {"router", "task-session", "full-chain"}:
                self._promote_model(
                    eval_type=eval_type,
                    source=source,
                    review=review,
                    staging=staging,
                )
            else:
                raise EvalError(f"unsupported promote type: {eval_type}")
            write_yaml(
                staging / "provenance.yaml",
                {
                    "schema_version": "eval_provenance_v1",
                    "promoted_at": utc_now_iso(),
                    "source": _provenance_source(eval_type, source),
                    "review_source": review.name,
                    "promoted_by": "local_user",
                },
            )
            if eval_type != "ingress":
                load_eval_case(
                    case_dir=staging,
                    case_type=eval_type,
                    run_config=self.loaded,
                )
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return target

    def _promote_model(
        self,
        *,
        eval_type: str,
        source: Path,
        review: Path,
        staging: Path,
    ) -> None:
        try:
            envelope = ReviewEnvelope.model_validate(read_yaml(review))
            expected_version = f"{eval_type.replace('-', '_')}_review_v1"
            if envelope.schema_version != expected_version:
                raise EvalError(f"review schema_version must be {expected_version}")
            scenario_data = dict(envelope.scenario)
            scenario_data.setdefault("schema_version", "eval_case_v1")
            scenario = scenario_model(eval_type).model_validate(scenario_data)
            labels_data = dict(envelope.labels)
            labels_data.setdefault(
                "schema_version", f"{eval_type.replace('-', '_')}_labels_v1"
            )
            labels = golden_labels_model(eval_type).model_validate(labels_data)
        except ValidationError as exc:
            raise EvalError(f"review is not promotable: {exc}") from exc
        raw_by_id = _raw_by_id(source / "messages.jsonl")
        referenced = scenario_message_ids(scenario)
        missing = [item for item in referenced if item not in raw_by_id]
        if missing:
            raise EvalError(f"promotion source is missing messages: {missing[:5]}")
        write_jsonl(
            staging / "messages.jsonl", [raw_by_id[item] for item in referenced]
        )
        write_yaml(staging / "eval_case.yaml", scenario.model_dump(mode="json"))
        write_yaml(staging / "labels.yaml", labels.model_dump(mode="json"))
        _copy_required(source, staging, "config.yaml", "metadata.yaml")
        for fixture in getattr(scenario, "resources", []):
            src = resource_fixture_path(source, fixture)
            if not src.is_file():
                raise EvalError(f"promotion resource is missing: {src}")
            dst = resource_fixture_path(staging, fixture)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def _promote_ingress(self, *, source: Path, review: Path, staging: Path) -> None:
        timeline = read_yaml(source / "ingress_timeline.yaml")
        labels = validate_ingress_review_labels(
            timeline=timeline, labels=read_yaml(review)
        )
        try:
            scenario = IngressScenario.model_validate(
                read_yaml(source / "eval_case.yaml")
            )
        except ValidationError as exc:
            raise EvalError(f"invalid ingress acquisition scenario: {exc}") from exc
        raws = read_jsonl(source / "raw_messages.jsonl")
        if any(not isinstance(row, dict) for row in raws):
            raise EvalError("ingress raw_messages.jsonl contains a non-object row")
        raw_ids = [message_id_from_raw(row) for row in raws]
        if not all(raw_ids) or len(raw_ids) != len(set(raw_ids)):
            raise EvalError(
                "ingress raw_messages.jsonl contains missing or duplicate message ids"
            )
        timeline_ids = {
            str(row.get("message_id"))
            for row in timeline.get("messages", [])
            if isinstance(row, dict)
        }
        if timeline_ids != set(raw_ids):
            raise EvalError("ingress timeline must exactly cover raw messages")
        write_jsonl(staging / "raw_messages.jsonl", raws)
        write_yaml(staging / "ingress_timeline.yaml", timeline)
        write_yaml(staging / "eval_case.yaml", scenario.model_dump(mode="json"))
        write_yaml(
            staging / "labels.yaml",
            {
                "schema_version": "ingress_golden_labels_v1",
                "labels": labels,
            },
        )
        _copy_required(source, staging, "config.yaml", "metadata.yaml")


def _raw_by_id(path: Path) -> dict[str, dict[str, Any]]:
    rows = read_jsonl(path)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise EvalError(f"{path} contains a non-object row")
        message_id = message_id_from_raw(row)
        if not message_id:
            raise EvalError(f"{path} contains a message without message_id")
        if message_id in result:
            raise EvalError(f"{path} contains duplicate message_id: {message_id}")
        result[message_id] = row
    return result


def _copy_required(source: Path, destination: Path, *names: str) -> None:
    for name in names:
        src = source / name
        if not src.is_file():
            raise EvalError(f"promotion source is missing {name}: {source}")
        shutil.copy2(src, destination / name)


def _provenance_source(eval_type: str, source: Path) -> dict[str, str]:
    if eval_type == "ingress":
        return {"kind": "ingress_run", "run_id": source.name}
    return {"kind": "capture", "case_id": source.name}


def _validate_source_owner(source: Path, loaded: LoadedConfig) -> None:
    config = read_yaml(source / "config.yaml")
    owner = config.get("owner")
    owner_open_id = owner.get("open_id") if isinstance(owner, dict) else None
    if owner_open_id != loaded.config.owner.open_id:
        raise EvalError(
            "promotion source owner.open_id must match Evaluation Run Config"
        )


def _validate_source_config_copy(source: Path, *, allow_sensitive_config: bool) -> None:
    paths = sensitive_config_paths(read_yaml(source / "config.yaml"))
    if paths and not allow_sensitive_config:
        raise EvalError(
            "promotion source config.yaml appears to contain sensitive fields. "
            "Re-run with --allow-sensitive-config to copy it anyway: "
            + ", ".join(paths)
        )
