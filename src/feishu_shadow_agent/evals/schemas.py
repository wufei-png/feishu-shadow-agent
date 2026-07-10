from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MessageSource = Literal["group_at_me", "active_watch", "p2p"]
TaskStatus = Literal["watching", "closed", "closed_by_owner", "human_taken_over"]
RouterRoute = Literal[
    "new_task",
    "attach_task",
    "reopen_task",
    "ignore",
    "ambiguous",
    "human_taken_over",
]
Answerability = Literal["auto_reply", "needs_owner", "no_reply"]
WatchAction = Literal["keep_watching", "close"]
IngressDecisionValue = Literal["kept", "dropped"]

TASK_ROUTES = {"new_task", "attach_task", "reopen_task"}
TASK_KEY_ROUTES = {"attach_task", "reopen_task", "human_taken_over"}


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceFixture(EvalModel):
    message_id: str = Field(min_length=1)
    file_key: str = Field(min_length=1)
    resource_type: Literal["file", "image"]
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class IngressActiveTaskFixture(EvalModel):
    chat_id: str = Field(min_length=1)
    thread_id: str | None = None
    watch_keys: list[str] = Field(min_length=1)


class IngressAcquisitionScenario(EvalModel):
    active_tasks: dict[str, IngressActiveTaskFixture] = Field(default_factory=dict)


class IngressScenario(EvalModel):
    schema_version: Literal["eval_case_v1"] = "eval_case_v1"
    case_type: Literal["ingress"] = "ingress"
    acquisition: IngressAcquisitionScenario = Field(
        default_factory=IngressAcquisitionScenario
    )


class RouterTarget(EvalModel):
    message_id: str = Field(min_length=1)
    source: MessageSource


class RouterTaskFixture(EvalModel):
    status: TaskStatus
    task_label: str | None
    message_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_messages(self) -> RouterTaskFixture:
        _require_unique(self.message_ids, "task fixture message_ids")
        return self


class RouterScenario(EvalModel):
    schema_version: Literal["eval_case_v1"] = "eval_case_v1"
    case_type: Literal["router"] = "router"
    target: RouterTarget
    tasks: dict[str, RouterTaskFixture] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_task_partition(self) -> RouterScenario:
        if any(not alias.strip() for alias in self.tasks):
            raise ValueError("router task aliases must be non-empty")
        message_ids = [
            message_id
            for task in self.tasks.values()
            for message_id in task.message_ids
        ]
        _require_unique(message_ids, "router task message partition")
        if self.target.message_id in message_ids:
            raise ValueError("router target must not already belong to a task fixture")
        return self


class TaskSessionScenario(EvalModel):
    schema_version: Literal["eval_case_v1"] = "eval_case_v1"
    case_type: Literal["task-session"] = "task-session"
    mode: Literal["initial", "resume"]
    message_ids: list[str] | None = None
    setup_message_ids: list[str] | None = None
    target_message_id: str | None = None
    resources: list[ResourceFixture] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mode_fields(self) -> TaskSessionScenario:
        if self.mode == "initial":
            if not self.message_ids:
                raise ValueError("initial mode requires non-empty message_ids")
            if self.setup_message_ids is not None or self.target_message_id is not None:
                raise ValueError(
                    "initial mode does not accept setup_message_ids or target_message_id"
                )
            _require_unique(self.message_ids, "message_ids")
            return self
        if not self.setup_message_ids or not self.target_message_id:
            raise ValueError(
                "resume mode requires setup_message_ids and target_message_id"
            )
        if self.message_ids is not None:
            raise ValueError("resume mode does not accept message_ids")
        ids = [*self.setup_message_ids, self.target_message_id]
        _require_unique(ids, "resume message ids")
        return self


class FullChainMessage(EvalModel):
    message_id: str = Field(min_length=1)
    source: MessageSource


class FullChainScenario(EvalModel):
    schema_version: Literal["eval_case_v1"] = "eval_case_v1"
    case_type: Literal["full-chain"] = "full-chain"
    setup: list[FullChainMessage] = Field(default_factory=list)
    target: FullChainMessage
    resources: list[ResourceFixture] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_messages(self) -> FullChainScenario:
        _require_unique(
            [*(item.message_id for item in self.setup), self.target.message_id],
            "full-chain message ids",
        )
        return self


Scenario = IngressScenario | RouterScenario | TaskSessionScenario | FullChainScenario


class DraftRouterLabels(EvalModel):
    route: RouterRoute | None = None
    task_key: str | None = None


class RouterLabels(EvalModel):
    schema_version: Literal["router_labels_v1"] = "router_labels_v1"
    route: RouterRoute
    task_key: str | None = None

    @model_validator(mode="after")
    def validate_task_key(self) -> RouterLabels:
        _validate_route_task_key(self.route, self.task_key)
        return self


class DraftTaskSessionLabels(EvalModel):
    reference_answer: str | None = None
    answerability: Answerability | None = None
    watch_action: WatchAction | None = None


class TaskSessionLabels(EvalModel):
    schema_version: Literal["task_session_labels_v1"] = "task_session_labels_v1"
    reference_answer: str | None = None
    answerability: Answerability
    watch_action: WatchAction

    @model_validator(mode="after")
    def validate_reference_answer(self) -> TaskSessionLabels:
        _validate_reference(self.answerability, self.reference_answer)
        return self


class DraftFullChainTaskSessionLabels(EvalModel):
    answerability: Answerability | None = None
    watch_action: WatchAction | None = None


class FullChainTaskSessionLabels(EvalModel):
    answerability: Answerability
    watch_action: WatchAction


class DraftFullChainRouterLabels(EvalModel):
    route: RouterRoute | None = None
    task_key: str | None = None


class FullChainRouterLabels(EvalModel):
    route: RouterRoute
    task_key: str | None = None

    @model_validator(mode="after")
    def validate_task_key(self) -> FullChainRouterLabels:
        _validate_route_task_key(self.route, self.task_key)
        return self


class DraftFullChainLabels(EvalModel):
    router: DraftFullChainRouterLabels = Field(
        default_factory=DraftFullChainRouterLabels
    )
    task_session: DraftFullChainTaskSessionLabels | None = None
    reference_answer: str | None = None


class FullChainLabels(EvalModel):
    schema_version: Literal["full_chain_labels_v1"] = "full_chain_labels_v1"
    router: FullChainRouterLabels
    task_session: FullChainTaskSessionLabels | None = None
    reference_answer: str | None = None

    @model_validator(mode="after")
    def validate_task_session(self) -> FullChainLabels:
        should_run = self.router.route in TASK_ROUTES
        if should_run and self.task_session is None:
            raise ValueError(f"{self.router.route} requires task_session labels")
        if not should_run and self.task_session is not None:
            raise ValueError(f"{self.router.route} must omit task_session labels")
        if self.task_session is None:
            if self.reference_answer is not None:
                raise ValueError(f"{self.router.route} must omit reference_answer")
            return self
        _validate_reference(self.task_session.answerability, self.reference_answer)
        return self


class ReviewEnvelope(EvalModel):
    schema_version: str = Field(min_length=1)
    scenario: dict[str, Any]
    labels: dict[str, Any]


class IngressReviewLabel(EvalModel):
    message_id: str = Field(min_length=1)
    timeline_index: int = Field(gt=0)
    sent_at: str | None = None
    sender_name: str | None = None
    text_excerpt: str = ""
    current_decision: IngressDecisionValue
    reason_code: str = Field(min_length=1)
    expected_decision: IngressDecisionValue
    review_reason: str = ""


class IngressReviewLabels(EvalModel):
    schema_version: Literal["ingress_review_labels_v1"] = "ingress_review_labels_v1"
    source_run: str = Field(min_length=1)
    labels: list[IngressReviewLabel]


class IngressGoldenLabel(EvalModel):
    message_id: str = Field(min_length=1)
    expected_decision: IngressDecisionValue
    review_reason: str = ""


class IngressGoldenLabels(EvalModel):
    schema_version: Literal["ingress_golden_labels_v1"] = "ingress_golden_labels_v1"
    labels: list[IngressGoldenLabel]

    @model_validator(mode="after")
    def validate_unique_messages(self) -> IngressGoldenLabels:
        _require_unique(
            [label.message_id for label in self.labels],
            "ingress golden label message_ids",
        )
        return self


class IngressJudgeLabel(EvalModel):
    message_id: str = Field(min_length=1)
    expected_decision: IngressDecisionValue
    review_reason: str = ""


class IngressJudgeOutput(EvalModel):
    labels: list[IngressJudgeLabel]


class SemanticDifference(EvalModel):
    type: Literal["omission", "unsupported_addition", "contradiction", "overcommitment"]
    severity: Literal["minor", "major", "critical"]
    summary: str = Field(min_length=1)


class SemanticJudgeOutput(EvalModel):
    verdict: Literal["pass", "partial", "fail"]
    differences: list[SemanticDifference]

    @model_validator(mode="after")
    def validate_verdict(self) -> SemanticJudgeOutput:
        if self.verdict == "pass" and self.differences:
            raise ValueError("pass verdict requires empty differences")
        if self.verdict != "pass" and not self.differences:
            raise ValueError(f"{self.verdict} verdict requires differences")
        if self.verdict == "partial" and any(
            item.severity == "critical" for item in self.differences
        ):
            raise ValueError("partial verdict cannot contain critical differences")
        return self


class CaptureProvenanceSource(EvalModel):
    kind: Literal["capture"]
    case_id: str = Field(min_length=1)


class IngressProvenanceSource(EvalModel):
    kind: Literal["ingress_run"]
    run_id: str = Field(min_length=1)


class EvalProvenance(EvalModel):
    schema_version: Literal["eval_provenance_v1"] = "eval_provenance_v1"
    promoted_at: str = Field(min_length=1)
    source: CaptureProvenanceSource | IngressProvenanceSource
    review_source: str = Field(min_length=1)
    promoted_by: Literal["local_user"]


def scenario_model(case_type: str) -> type[EvalModel]:
    models: dict[str, type[EvalModel]] = {
        "ingress": IngressScenario,
        "router": RouterScenario,
        "task-session": TaskSessionScenario,
        "full-chain": FullChainScenario,
    }
    try:
        return models[case_type]
    except KeyError as exc:
        raise ValueError(f"unsupported eval case type: {case_type}") from exc


def draft_labels_model(case_type: str) -> type[EvalModel]:
    models: dict[str, type[EvalModel]] = {
        "router": DraftRouterLabels,
        "task-session": DraftTaskSessionLabels,
        "full-chain": DraftFullChainLabels,
    }
    try:
        return models[case_type]
    except KeyError as exc:
        raise ValueError(f"unsupported draft labels type: {case_type}") from exc


def golden_labels_model(case_type: str) -> type[EvalModel]:
    models: dict[str, type[EvalModel]] = {
        "router": RouterLabels,
        "task-session": TaskSessionLabels,
        "full-chain": FullChainLabels,
    }
    try:
        return models[case_type]
    except KeyError as exc:
        raise ValueError(f"unsupported golden labels type: {case_type}") from exc


def _validate_route_task_key(route: str, task_key: str | None) -> None:
    if route in TASK_KEY_ROUTES and not (task_key and task_key.strip()):
        raise ValueError(f"{route} requires task_key")
    if route not in TASK_KEY_ROUTES and task_key is not None:
        raise ValueError(f"{route} must omit task_key")


def _validate_reference(
    answerability: Answerability, reference_answer: str | None
) -> None:
    if answerability == "no_reply":
        if reference_answer is not None:
            raise ValueError("no_reply must omit reference_answer")
        return
    if not (reference_answer and reference_answer.strip()):
        raise ValueError(f"{answerability} requires non-empty reference_answer")


def _require_unique(values: list[str], field: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must not contain duplicates")
