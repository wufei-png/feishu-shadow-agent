from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ..config import LoadedConfig
from ..ingestion import MessageNormalizer, normalize_message_sent_at
from .artifacts import (
    EvalError,
    file_sha256,
    message_id_from_raw,
    read_jsonl,
    read_yaml,
)
from .schemas import (
    EvalModel,
    EvalProvenance,
    FullChainScenario,
    ResourceFixture,
    ReviewEnvelope,
    RouterScenario,
    TaskSessionScenario,
    draft_labels_model,
    golden_labels_model,
    scenario_model,
)

LabelStatus = Literal["none", "draft", "golden"]


@dataclass(frozen=True)
class LoadedEvalCase:
    directory: Path
    case_type: str
    status: LabelStatus
    scenario: EvalModel
    labels: EvalModel | None
    raw_messages: dict[str, dict[str, Any]]
    case_config_hash: str


def load_eval_case(
    *, case_dir: Path, case_type: str, run_config: LoadedConfig
) -> LoadedEvalCase:
    directory = case_dir.expanduser().resolve()
    if not directory.is_dir():
        raise EvalError(f"eval case directory does not exist: {directory}")
    config_path = directory / "config.yaml"
    if not config_path.is_file():
        raise EvalError(f"eval case is missing config.yaml: {directory}")
    _validate_owner(config_path=config_path, run_config=run_config)

    review_path = directory / f"{case_type.replace('-', '_')}.review.yaml"
    scenario_path = directory / "eval_case.yaml"
    labels_path = directory / "labels.yaml"
    provenance_path = directory / "provenance.yaml"
    if review_path.exists():
        if any(path.exists() for path in (scenario_path, labels_path, provenance_path)):
            raise EvalError(
                f"draft review cannot be mixed with split case files: {directory}"
            )
        envelope = _validate(ReviewEnvelope, read_yaml(review_path), review_path)
        expected_version = f"{case_type.replace('-', '_')}_review_v1"
        if envelope.schema_version != expected_version:
            raise EvalError(f"{review_path} schema_version must be {expected_version}")
        scenario_data = dict(envelope.scenario)
        scenario_data.setdefault("schema_version", "eval_case_v1")
        scenario = _validate(scenario_model(case_type), scenario_data, review_path)
        labels = _validate(draft_labels_model(case_type), envelope.labels, review_path)
        status: LabelStatus = "draft"
    else:
        if not scenario_path.exists():
            raise EvalError(f"eval case requires {review_path.name} or eval_case.yaml")
        scenario = _validate(
            scenario_model(case_type), read_yaml(scenario_path), scenario_path
        )
        if labels_path.exists() != provenance_path.exists():
            raise EvalError(
                "labels.yaml and provenance.yaml must either both exist or both be absent"
            )
        if labels_path.exists():
            labels = _validate(
                golden_labels_model(case_type), read_yaml(labels_path), labels_path
            )
            provenance = _validate(
                EvalProvenance,
                read_yaml(provenance_path),
                provenance_path,
            )
            if provenance.source.kind != "capture":
                raise EvalError("model golden provenance source must be capture")
            _parse_aware_datetime(provenance.promoted_at, field="promoted_at")
            status = "golden"
        else:
            labels = None
            status = "none"

    messages_path = directory / "messages.jsonl"
    rows = read_jsonl(messages_path)
    raw_messages: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise EvalError(f"{messages_path} rows must be JSON objects")
        message_id = message_id_from_raw(row)
        if not message_id:
            raise EvalError(f"{messages_path} contains a message without message_id")
        if message_id in raw_messages:
            raise EvalError(f"duplicate raw message_id: {message_id}")
        raw_messages[message_id] = row

    referenced = scenario_message_ids(scenario)
    missing = [
        message_id for message_id in referenced if message_id not in raw_messages
    ]
    if missing:
        raise EvalError(f"scenario messages missing from messages.jsonl: {missing[:5]}")
    _validate_case_relationships(
        scenario,
        labels,
        raw_messages=raw_messages,
        run_config=run_config,
    )
    _validate_scenario_time_order(scenario, raw_messages)
    _validate_resources(directory, scenario, raw_messages)
    return LoadedEvalCase(
        directory=directory,
        case_type=case_type,
        status=status,
        scenario=scenario,
        labels=labels,
        raw_messages=raw_messages,
        case_config_hash=file_sha256(config_path),
    )


def scenario_message_ids(scenario: EvalModel) -> list[str]:
    if isinstance(scenario, RouterScenario):
        return list(
            dict.fromkeys(
                [
                    *(
                        message_id
                        for task in scenario.tasks.values()
                        for message_id in task.message_ids
                    ),
                    scenario.target.message_id,
                ]
            )
        )
    if isinstance(scenario, TaskSessionScenario):
        if scenario.mode == "initial":
            return list(scenario.message_ids or [])
        return [*(scenario.setup_message_ids or []), str(scenario.target_message_id)]
    if isinstance(scenario, FullChainScenario):
        return [
            *(item.message_id for item in scenario.setup),
            scenario.target.message_id,
        ]
    raise EvalError(f"unsupported model case scenario: {type(scenario).__name__}")


def resource_fixture_path(case_dir: Path, fixture: ResourceFixture) -> Path:
    from hashlib import sha256

    message_part = _safe_path_part(fixture.message_id)
    key_hash = sha256(fixture.file_key.encode("utf-8")).hexdigest()[:12]
    return (
        case_dir
        / "resources"
        / message_part
        / f"{fixture.resource_type}_{key_hash}.bin"
    )


def message_sent_at(raw: dict[str, Any]) -> str:
    for key in ("create_time", "created_at", "sent_at", "timestamp"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            normalized = normalize_message_sent_at(value)
            assert normalized is not None
            _parse_aware_datetime(normalized)
            return normalized
    raise EvalError(f"message {message_id_from_raw(raw)} is missing sent_at")


def _validate_owner(*, config_path: Path, run_config: LoadedConfig) -> None:
    raw = read_yaml(config_path)
    owner = raw.get("owner")
    baseline_owner = owner.get("open_id") if isinstance(owner, dict) else None
    if baseline_owner != run_config.config.owner.open_id:
        raise EvalError(
            "case config owner.open_id must match Evaluation Run Config owner.open_id"
        )


def _validate_case_relationships(
    scenario: EvalModel,
    labels: EvalModel | None,
    *,
    raw_messages: dict[str, dict[str, Any]],
    run_config: LoadedConfig,
) -> None:
    if not isinstance(scenario, RouterScenario):
        return
    if labels is not None:
        task_key = getattr(labels, "task_key", None)
        if task_key is not None and task_key not in scenario.tasks:
            raise EvalError(
                f"router label task_key is not defined by scenario: {task_key}"
            )
    target_time = _message_datetime(raw_messages[scenario.target.message_id])
    watch_minutes = run_config.config.lifecycle.watch_minutes
    for alias, fixture in scenario.tasks.items():
        if fixture.status != "watching":
            continue
        last_message_time = _message_datetime(raw_messages[fixture.message_ids[-1]])
        if last_message_time + timedelta(minutes=watch_minutes) <= target_time:
            raise EvalError(f"watching task fixture is expired at target: {alias}")


def _validate_scenario_time_order(
    scenario: EvalModel, raw_messages: dict[str, dict[str, Any]]
) -> None:
    if isinstance(scenario, RouterScenario):
        target_dt = _message_datetime(raw_messages[scenario.target.message_id])
        for alias, task in scenario.tasks.items():
            values = [
                _message_datetime(raw_messages[item]) for item in task.message_ids
            ]
            _require_strictly_increasing(values, f"task {alias} message_ids")
            if values[-1] >= target_dt:
                raise EvalError(f"task {alias} messages must precede router target")
        _validate_router_task_chats(scenario, raw_messages)
        return
    if isinstance(scenario, TaskSessionScenario):
        if scenario.mode == "initial":
            values = [
                _message_datetime(raw_messages[item])
                for item in scenario.message_ids or []
            ]
            _require_strictly_increasing(values, "task-session message_ids")
        else:
            setup_raws = [
                raw_messages[item] for item in scenario.setup_message_ids or []
            ]
            _require_increasing_message_order(
                setup_raws, "task-session setup_message_ids"
            )
            setup = [_message_datetime(raw) for raw in setup_raws]
            target = _message_datetime(raw_messages[str(scenario.target_message_id)])
            if setup[-1] >= target:
                raise EvalError("task-session target must be later than setup messages")
        _validate_task_session_chat(scenario, raw_messages)
        return
    if isinstance(scenario, FullChainScenario):
        values = [
            *(
                _message_datetime(raw_messages[item.message_id])
                for item in scenario.setup
            ),
            _message_datetime(raw_messages[scenario.target.message_id]),
        ]
        _require_strictly_increasing(values, "full-chain setup and target")


def _validate_resources(
    directory: Path,
    scenario: EvalModel,
    raw_messages: dict[str, dict[str, Any]],
) -> None:
    fixtures = getattr(scenario, "resources", [])
    seen: set[tuple[str, str, str]] = set()
    for fixture in fixtures:
        key = (fixture.message_id, fixture.file_key, fixture.resource_type)
        if key in seen:
            raise EvalError(f"duplicate resource fixture: {key}")
        seen.add(key)
        if fixture.message_id not in raw_messages:
            raise EvalError(
                f"resource fixture message is not in messages.jsonl: {fixture.message_id}"
            )
        path = resource_fixture_path(directory, fixture)
        if not path.is_file():
            raise EvalError(f"resource fixture file is missing: {path}")
        if file_sha256(path).lower() != fixture.sha256.lower():
            raise EvalError(f"resource fixture sha256 mismatch: {path}")
    if not isinstance(scenario, (TaskSessionScenario, FullChainScenario)):
        if fixtures:
            raise EvalError(
                "resource fixtures are only valid for task-session/full-chain"
            )
        return
    owner_open_id = _baseline_owner(directory / "config.yaml")
    normalizer = MessageNormalizer(owner_open_id=owner_open_id)
    referenced_ids = set(scenario_message_ids(scenario))
    raw_refs = {
        (resource.message_id, resource.file_key, resource.resource_type)
        for message_id in referenced_ids
        for resource in normalizer.normalize(raw_messages[message_id]).resources
    }
    if seen != raw_refs:
        missing = sorted(raw_refs - seen)
        extra = sorted(seen - raw_refs)
        raise EvalError(
            "resource fixtures must exactly match scenario message resources; "
            f"missing={missing[:5]}, extra={extra[:5]}"
        )


def _validate(model: type[EvalModel], data: Any, path: Path) -> EvalModel:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise EvalError(f"invalid {path}: {exc}") from exc


def _message_datetime(raw: dict[str, Any]) -> datetime:
    return _parse_aware_datetime(message_sent_at(raw))


def _parse_aware_datetime(value: str, *, field: str = "message sent_at") -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvalError(f"invalid {field}: {value}") from exc
    if parsed.utcoffset() is None:
        raise EvalError(f"{field} must include timezone: {value}")
    return parsed


def _require_strictly_increasing(values: list[datetime], field: str) -> None:
    if any(
        current >= following
        for current, following in zip(values, values[1:], strict=False)
    ):
        raise EvalError(f"{field} must be strictly increasing by sent_at")


def _require_increasing_message_order(raws: list[dict[str, Any]], field: str) -> None:
    keys = [(_message_datetime(raw), _message_position(raw)) for raw in raws]
    if any(
        current >= following for current, following in zip(keys, keys[1:], strict=False)
    ):
        raise EvalError(f"{field} must follow sent_at and message_position order")


def _message_position(raw: dict[str, Any]) -> int:
    value = raw.get("message_position")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _safe_path_part(value: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in value
    )
    return cleaned[:120] or "resource"


def _baseline_owner(config_path: Path) -> str:
    raw = read_yaml(config_path)
    owner = raw.get("owner")
    value = owner.get("open_id") if isinstance(owner, dict) else None
    if not isinstance(value, str) or not value:
        raise EvalError(f"case config is missing owner.open_id: {config_path}")
    return value


def _validate_task_session_chat(
    scenario: TaskSessionScenario,
    raw_messages: dict[str, dict[str, Any]],
) -> None:
    owner_open_id = "eval-owner"
    normalizer = MessageNormalizer(owner_open_id=owner_open_id)
    messages = [
        normalizer.normalize(raw_messages[message_id])
        for message_id in scenario_message_ids(scenario)
    ]
    chats = {(message.chat_id, message.chat_type) for message in messages}
    if len(chats) != 1:
        raise EvalError("task-session scenario messages must share chat_id/chat_type")


def _validate_router_task_chats(
    scenario: RouterScenario,
    raw_messages: dict[str, dict[str, Any]],
) -> None:
    normalizer = MessageNormalizer(owner_open_id="eval-owner")
    for alias, fixture in scenario.tasks.items():
        chats = {
            (message.chat_id, message.chat_type)
            for message in (
                normalizer.normalize(raw_messages[message_id])
                for message_id in fixture.message_ids
            )
        }
        if len(chats) != 1:
            raise EvalError(
                f"router task {alias} messages must share chat_id/chat_type"
            )
