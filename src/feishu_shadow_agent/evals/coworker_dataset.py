from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    model_validator,
)

from .artifacts import EvalError


class _ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewMessage(_ReviewModel):
    role: Literal["机器人", "用户本人", "同事"]
    timestamp: str
    text: str
    scope: Literal["source_chat", "related_chat"] = "source_chat"


class ReviewForwardedMessage(_ReviewModel):
    role: Literal["同事", "用户本人"]
    message_type: Literal["text", "post"]
    text: str = Field(min_length=1)


class ReviewAsset(_ReviewModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["file", "image"]
    access: str


class ReviewEvidence(_ReviewModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    repository: str
    revision: str
    revision_committed_at: str
    source_path: str
    scope: str
    temporal_basis: Literal["source_time", "evaluation_time"] = "source_time"
    lookup_kind: Literal["file", "gitlab_mr"] = "file"
    lookup_url: str | None = None

    @model_validator(mode="after")
    def validate_lookup(self) -> ReviewEvidence:
        if (self.lookup_kind == "gitlab_mr") != bool(self.lookup_url):
            raise ValueError("GitLab MR fixtures require a lookup URL")
        return self


class ReviewRepository(_ReviewModel):
    repository: str
    mount_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    tree_sha1: str = Field(pattern=r"^[a-f0-9]{40}$")
    revision_committed_at: str
    temporal_basis: Literal["source_time", "evaluation_time"]

    @model_validator(mode="after")
    def validate_path(self) -> ReviewRepository:
        if not Path(self.repository).is_absolute():
            raise ValueError("review repository must be an absolute path")
        return self


class ReviewSupportFile(_ReviewModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    install_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_install_path(self) -> ReviewSupportFile:
        path = Path(self.install_path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("support file install path must be absolute")
        return self


class SourceResource(_ReviewModel):
    source_message_id: str
    raw_local_path: str
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_input_path: str | None
    excluded_from_model: bool
    source_timing: str | None

    @model_validator(mode="after")
    def validate_excluded(self) -> SourceResource:
        if self.excluded_from_model and self.model_input_path is not None:
            raise ValueError("an excluded source cannot have a model input path")
        return self


class SourceReview(_ReviewModel):
    message_id: str
    chat_id: str
    source_time: str
    source_app_link: str | None
    current_snapshot_edited: bool
    snapshot_update_time: str | None
    original_text: str
    context_source_message_ids: list[str]
    resources: list[SourceResource] = Field(
        default_factory=lambda: list[SourceResource]()
    )


class CoworkerScenario(_ReviewModel):
    chat_type: Literal["group", "private"]
    trigger: Literal[
        "direct_mention", "at_all", "quote_without_mention", "private_owner"
    ]
    message: str = Field(min_length=1)
    forwarded_message: ReviewForwardedMessage | None = None
    history_available_to_tools: list[ReviewMessage]
    quoted_message: ReviewMessage | None
    assets: list[ReviewAsset]
    read_only_evidence: list[ReviewEvidence]
    read_only_repositories: list[ReviewRepository] = Field(
        default_factory=lambda: list[ReviewRepository]()
    )
    linked_topic_fixture: list[ReviewMessage]
    mode_config: dict[str, str | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_chat_trigger(self) -> CoworkerScenario:
        if (self.chat_type == "private") != (self.trigger == "private_owner"):
            raise ValueError("private scenarios must use the private owner trigger")
        if self.trigger == "quote_without_mention" and self.quoted_message is None:
            raise ValueError("a quote trigger requires the quoted message")
        if self.forwarded_message is not None and self.chat_type != "private":
            raise ValueError("forwarded requests must be private")
        mounts = [item.mount_name for item in self.read_only_repositories]
        if len(mounts) != len(set(mounts)):
            raise ValueError("review repository mount names must be unique")
        return self


class ReviewSkillExpectation(_ReviewModel):
    name: str = Field(min_length=1)
    requirement: Literal["required", "optional", "not_required"]
    rationale: str = Field(min_length=1)


class ReviewExpectation(_ReviewModel):
    trigger: Literal["reply_or_task", "no_reply", "mode_specific"]
    criteria: list[str] = Field(min_length=1)
    reference_status: str
    reference_reply: str | None = None
    skill_expectations: list[ReviewSkillExpectation] = Field(
        default_factory=lambda: list[ReviewSkillExpectation]()
    )

    @model_validator(mode="after")
    def validate_skill_names(self) -> ReviewExpectation:
        names = [item.name for item in self.skill_expectations]
        if len(names) != len(set(names)):
            raise ValueError("skill expectation names must be unique")
        return self


class CaseAudit(_ReviewModel):
    assistant_review: str
    review_note: str
    human_review: str
    excluded_raw_image: str | None = None
    excluded_quote: str | None = None


class CoworkerReviewCase(_ReviewModel):
    case_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    title: str = Field(min_length=1)
    source_kind: Literal[
        "group_work", "private_work", "peer_work", "bot_history", "synthetic"
    ]
    source_review: SourceReview
    scenario: CoworkerScenario
    expected: ReviewExpectation
    audit: CaseAudit

    @model_validator(mode="after")
    def validate_source(self) -> CoworkerReviewCase:
        if self.source_kind != "synthetic" and (self.source_kind == "private_work") != (
            self.scenario.chat_type == "private"
        ):
            raise ValueError("source kind and scenario chat type do not match")
        return self


class CoworkerReviewDataset(_ReviewModel):
    schema_version: Literal[
        "coworker_comparison_review_v1", "coworker_comparison_review_v2"
    ]
    dataset_id: str = Field(min_length=1)
    state: Literal["draft_not_approved"]
    window_start: str
    window_end: str
    rules: list[str]
    support_files: list[ReviewSupportFile] = Field(
        default_factory=lambda: list[ReviewSupportFile]()
    )
    cases: list[CoworkerReviewCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_ids(self) -> CoworkerReviewDataset:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        install_paths = [item.install_path for item in self.support_files]
        if len(install_paths) != len(set(install_paths)):
            raise ValueError("support file install paths must be unique")
        return self


@dataclass(frozen=True)
class InspectedCoworkerReview:
    dataset: CoworkerReviewDataset
    dataset_sha256: str
    review_snapshot_sha256: str
    # Bytes are captured during inspection so a future runner need not reopen mutable files.
    input_files: dict[str, bytes]

    def summary(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset.dataset_id,
            "case_count": len(self.dataset.cases),
            "source_counts": dict(
                Counter(case.source_kind for case in self.dataset.cases)
            ),
            "dataset_sha256": self.dataset_sha256,
            "review_snapshot_sha256": self.review_snapshot_sha256,
            "input_file_count": len(self.input_files),
            "state": self.dataset.state,
            "real_model_authorized": False,
        }

    def replay_input(self, case_id: str) -> dict[str, Any]:
        """Separate the event from tool-only context and human review material."""
        case = next(
            (item for item in self.dataset.cases if item.case_id == case_id), None
        )
        if case is None:
            raise EvalError(f"unknown coworker case: {case_id}")
        scenario = case.scenario
        placements = _asset_placements(case)
        return {
            "case_id": case.case_id,
            "event": {
                "id": "event-001",
                "chat_type": scenario.chat_type,
                "trigger": scenario.trigger,
                "message": scenario.message,
                "forwarded_message": scenario.forwarded_message.model_dump()
                if scenario.forwarded_message
                else None,
                "quoted_message": {
                    "id": "quote-001",
                    **scenario.quoted_message.model_dump(),
                }
                if scenario.quoted_message
                else None,
            },
            "tool_fixtures": {
                "support_files": [
                    item.model_dump() for item in self.dataset.support_files
                ],
                "history": [
                    {"id": f"history-{index:03d}", **item.model_dump()}
                    for index, item in enumerate(scenario.history_available_to_tools, 1)
                ],
                "linked_topic": [
                    item.model_dump() for item in scenario.linked_topic_fixture
                ],
                "assets": [
                    {
                        "path": item.path,
                        "sha256": item.sha256,
                        "kind": item.kind,
                        "placement": placements[item.path],
                    }
                    for item in scenario.assets
                ],
                "evidence": [
                    {
                        "path": item.path,
                        "sha256": item.sha256,
                        "source_path": item.source_path,
                        "revision": item.revision,
                        "lookup_kind": item.lookup_kind,
                        "lookup_url": item.lookup_url,
                    }
                    for item in scenario.read_only_evidence
                ],
                "repositories": [
                    {
                        "mount_name": item.mount_name,
                        "revision": item.revision,
                        "tree_sha1": item.tree_sha1,
                    }
                    for item in scenario.read_only_repositories
                ],
            },
            # Other review metadata describes expected ingress behavior, not settings.
            "mode_config": {
                key: value
                for key, value in scenario.mode_config.items()
                if key in {"codex_reply_mode", "hermes_group_require_mention"}
            },
        }


class CoworkerReviewApproval(_ReviewModel):
    dataset_id: str = Field(min_length=1)
    review_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    user_approved: StrictBool
    assistant_approved: StrictBool
    review_note: str


def require_coworker_review_approval(
    review: InspectedCoworkerReview, path: Path
) -> CoworkerReviewApproval:
    """Reject model execution unless this exact inspected snapshot was reviewed."""
    try:
        approval = CoworkerReviewApproval.model_validate(
            yaml.safe_load(path.read_bytes())
        )
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise EvalError("coworker review approval is missing or invalid") from exc
    if (
        approval.dataset_id != review.dataset.dataset_id
        or approval.review_snapshot_sha256 != review.review_snapshot_sha256
        or not approval.user_approved
        or not approval.assistant_approved
    ):
        raise EvalError("coworker review snapshot lacks both approvals")
    return approval


def inspect_coworker_review(path: Path) -> InspectedCoworkerReview:
    """Inspect a draft only; this function never invokes an Agent or sends a message."""
    path = path.expanduser().resolve()
    root = path.parent
    try:
        dataset_bytes = path.read_bytes()
        review_bytes = (root / "REVIEW.md").read_bytes()
        sums_bytes = (root / "SHA256SUMS.review").read_bytes()
    except OSError as exc:
        raise EvalError(f"coworker review artifact is missing: {exc.filename}") from exc
    try:
        parsed: Any = yaml.safe_load(dataset_bytes)
        dataset = CoworkerReviewDataset.model_validate(parsed)
    except yaml.YAMLError as exc:
        raise EvalError("invalid coworker review YAML") from exc
    except ValidationError as exc:
        # Do not put private message contents from a validation error in CLI output.
        error = exc.errors(include_input=False)[0]
        location = ".".join(str(part) for part in error["loc"])
        raise EvalError(
            f"invalid coworker review dataset at {location}: {error['type']}"
        ) from exc

    try:
        review_text = review_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvalError("REVIEW.md is not UTF-8") from exc
    first_case = dataset.cases[0]
    marker = (
        "## 共用技能与风格资料\n"
        if dataset.support_files
        else f"## {first_case.case_id} · {first_case.title}\n"
    )
    preamble, separator, _ = review_text.partition(marker)
    if not separator or review_text != render_coworker_review(
        dataset, preamble=preamble
    ):
        raise EvalError("REVIEW.md does not match the dataset")

    try:
        window_start = datetime.fromisoformat(dataset.window_start)
        window_end = datetime.fromisoformat(dataset.window_end)
    except ValueError as exc:
        raise EvalError("invalid coworker review window") from exc
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise EvalError("coworker review window must include a timezone")
    if window_start > window_end:
        raise EvalError("coworker review window is reversed")
    input_files: dict[str, bytes] = {}
    digests = {path.name: _sha256(dataset_bytes), "REVIEW.md": _sha256(review_bytes)}
    for item in dataset.support_files:
        if item.path in {path.name, "REVIEW.md", "SHA256SUMS.review"}:
            raise EvalError(f"coworker support file is review material: {item.path}")
        content = _read_relative_file(root, item.path)
        if _sha256(content) != item.sha256:
            raise EvalError(f"coworker input hash mismatch: {item.path}")
        previous = input_files.setdefault(item.path, content)
        if previous != content:
            raise EvalError(f"coworker input changed during inspection: {item.path}")
        digests[item.path] = item.sha256
    for case in dataset.cases:
        target = _validate_context_times(case, window_start.tzinfo, window_end)
        if not window_start <= target <= window_end:
            raise EvalError(
                f"coworker target is outside the review window: {case.case_id}"
            )
        _asset_placements(case)
        for item in [*case.scenario.assets, *case.scenario.read_only_evidence]:
            content = _read_relative_file(root, item.path)
            if _sha256(content) != item.sha256:
                raise EvalError(f"coworker input hash mismatch: {item.path}")
            previous = input_files.setdefault(item.path, content)
            if previous != content:
                raise EvalError(
                    f"coworker input changed during inspection: {item.path}"
                )
            digests[item.path] = item.sha256
        for resource in case.source_review.resources:
            content = _read_relative_file(root, resource.raw_local_path)
            if _sha256(content) != resource.raw_sha256:
                raise EvalError(
                    f"coworker source hash mismatch: {resource.raw_local_path}"
                )
            matching = [
                item
                for item in [
                    *case.scenario.assets,
                    *case.scenario.read_only_evidence,
                    *dataset.support_files,
                ]
                if item.sha256 == resource.raw_sha256
            ]
            if resource.excluded_from_model and matching:
                raise EvalError(
                    f"excluded coworker source is a model input: {case.case_id}"
                )
            if resource.model_input_path is not None and not any(
                item.path == resource.model_input_path for item in case.scenario.assets
            ):
                raise EvalError(f"coworker source input is missing: {case.case_id}")
            if resource.model_input_path != resource.raw_local_path and matching:
                raise EvalError(f"raw coworker source is a model input: {case.case_id}")

    expected_sums = "".join(f"{digests[name]}  {name}\n" for name in sorted(digests))
    if sums_bytes != expected_sums.encode("utf-8"):
        raise EvalError(
            "SHA256SUMS.review does not match the reviewed dataset and inputs"
        )
    return InspectedCoworkerReview(
        dataset=dataset,
        dataset_sha256=_sha256(dataset_bytes),
        review_snapshot_sha256=_sha256(sums_bytes),
        input_files=input_files,
    )


def _asset_placements(case: CoworkerReviewCase) -> dict[str, dict[str, str]]:
    """Map original resource provenance to synthetic replay locations."""
    scenario, source = case.scenario, case.source_review
    history_count = len(scenario.history_available_to_tools)
    if len(set(source.context_source_message_ids)) != len(
        source.context_source_message_ids
    ):
        raise EvalError(f"coworker context provenance is duplicated: {case.case_id}")
    if scenario.chat_type == "group" and len(source.context_source_message_ids) != (
        history_count + int(scenario.quoted_message is not None)
    ):
        raise EvalError(f"coworker context provenance is incomplete: {case.case_id}")
    placements: dict[str, dict[str, str]] = {}
    for asset in scenario.assets:
        if asset.path in placements:
            raise EvalError(f"coworker asset is duplicated: {case.case_id}")
        sources = [
            item for item in source.resources if item.model_input_path == asset.path
        ]
        if len(sources) != 1:
            raise EvalError(f"coworker asset provenance is ambiguous: {case.case_id}")
        source_id = sources[0].source_message_id
        if scenario.chat_type == "private" or source_id == source.message_id:
            placements[asset.path] = {"kind": "event", "message_id": "event-001"}
        elif source_id in source.context_source_message_ids:
            index = source.context_source_message_ids.index(source_id)
            if index < history_count:
                placements[asset.path] = {
                    "kind": "history",
                    "message_id": f"history-{index + 1:03d}",
                }
            else:
                placements[asset.path] = {"kind": "quote", "message_id": "quote-001"}
        else:
            raise EvalError(f"coworker asset has no replay location: {case.case_id}")
    return placements


def _read_relative_file(root: Path, name: str) -> bytes:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise EvalError(f"coworker review file must stay inside the dataset: {name}")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise EvalError(f"coworker review file must stay inside the dataset: {name}")
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise EvalError(f"cannot read coworker review file: {name}") from exc


def _validate_context_times(
    case: CoworkerReviewCase, review_timezone: tzinfo, window_end: datetime
) -> datetime:
    try:
        target = _review_time(case.source_review.source_time, review_timezone)
        messages = [
            *case.scenario.history_available_to_tools,
            *case.scenario.linked_topic_fixture,
        ]
        if case.scenario.quoted_message is not None:
            messages.append(case.scenario.quoted_message)
        times = [
            _review_time(message.timestamp, review_timezone) for message in messages
        ]
        sources = [
            *case.scenario.read_only_evidence,
            *case.scenario.read_only_repositories,
        ]
        revisions = [
            datetime.fromisoformat(item.revision_committed_at) for item in sources
        ]
        if any(revision.tzinfo is None for revision in revisions):
            raise ValueError("Git revision timestamp has no timezone")
    except ValueError as exc:
        raise EvalError(f"invalid coworker review timestamp: {case.case_id}") from exc
    if any(time > target for time in times):
        raise EvalError(f"coworker context is later than target: {case.case_id}")
    if any(
        item.temporal_basis == "source_time" and revision > target
        for item, revision in zip(sources, revisions, strict=True)
    ):
        raise EvalError(f"coworker evidence is later than target: {case.case_id}")
    if any(
        item.temporal_basis == "evaluation_time" and revision > window_end
        for item, revision in zip(sources, revisions, strict=True)
    ):
        raise EvalError(
            f"coworker evidence is later than evaluation window: {case.case_id}"
        )
    return target


def _review_time(value: str, review_timezone: tzinfo) -> datetime:
    parsed = datetime.fromisoformat(value)
    return (
        parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=review_timezone)
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def render_coworker_review(dataset: CoworkerReviewDataset, *, preamble: str) -> str:
    """Render the exact human-visible review corresponding to a draft dataset."""
    lines: list[str] = []
    if dataset.support_files:
        lines.extend(
            [
                "## 共用技能与风格资料",
                "",
                "以下技能和风格档案快照供两种模式和全部案例只读使用，不代表已冻结全部运行时技能。"
                "路径和摘要供审核；标签与理由不会作为请求内容传给被测 Agent。",
                "",
            ]
        )
        lines.extend(
            f"- [{item.path}]({item.path}) · SHA-256 `{item.sha256}` · 安装位置 `{item.install_path}`"
            for item in dataset.support_files
        )
        lines.append("")
    for case in dataset.cases:
        source, scenario = case.source_review, case.scenario
        edited = (
            "已编辑，采用当前快照" if source.current_snapshot_edited else "未显示编辑"
        )
        source_link = (
            f"[原始消息]({source.source_app_link})"
            if source.source_app_link
            else "合成场景"
        )
        lines.extend(
            [
                f"## {case.case_id} · {case.title}",
                "",
                f"- 来源：`{case.source_kind}`；{source.source_time}；{source_link}。",
                f"- 目标快照：{edited}；触发：`{scenario.trigger}`。",
                f"- 初审说明：{case.audit.review_note}",
                "",
                "**原始目标消息**",
                "",
                source.original_text,
                "",
                "**拟输入给同事的目标消息**",
                "",
                scenario.message,
                "",
            ]
        )
        if scenario.mode_config:
            lines.extend(
                [
                    f"- 本例模式设置：`{json.dumps(scenario.mode_config, ensure_ascii=False)}`",
                    "",
                ]
            )
        if scenario.forwarded_message:
            forwarded = scenario.forwarded_message
            lines.extend(
                [
                    "**随请求转发的消息**",
                    "",
                    f"{forwarded.role}（{forwarded.message_type}）：{forwarded.text}",
                    "",
                ]
            )
        if scenario.history_available_to_tools:
            heading = (
                "**可通过历史检索的目标前消息**"
                if dataset.schema_version == "coworker_comparison_review_v2"
                and scenario.chat_type == "private"
                else "**同群可查的目标前上下文**"
            )
            lines.extend([heading, ""])
            lines.extend(
                f"- {item.timestamp} {item.role}"
                f"{'（关联会话）' if dataset.schema_version == 'coworker_comparison_review_v2' and item.scope == 'related_chat' else ''}：{item.text}"
                for item in scenario.history_available_to_tools
            )
            lines.append("")
        if scenario.quoted_message:
            item = scenario.quoted_message
            lines.extend(
                ["**引用消息**", "", f"{item.timestamp} {item.role}：{item.text}", ""]
            )
        if scenario.linked_topic_fixture:
            lines.extend(["**同群目标前的话题夹具（仅在读取时提供）**", ""])
            lines.extend(
                f"- {item.timestamp} {item.role}：{item.text}"
                for item in scenario.linked_topic_fixture
            )
            lines.append("")
        if scenario.assets or scenario.read_only_evidence:
            lines.extend(["**允许读取的附件与证据**", ""])
            for item in [*scenario.assets, *scenario.read_only_evidence]:
                label = f"- [{item.path}]({item.path}) · SHA-256 `{item.sha256}`"
                if isinstance(item, ReviewEvidence):
                    label += f" · Git `{item.revision}` · `{item.source_path}`"
                    if dataset.schema_version == "coworker_comparison_review_v1":
                        label += "；现场版本未知"
                    else:
                        label += f"；依据：{'消息当时' if item.temporal_basis == 'source_time' else '评测时版本'}"
                        if item.lookup_url:
                            label += (
                                f"；按需检索 [{item.lookup_kind}]({item.lookup_url})"
                            )
                lines.append(label)
            lines.append("")
        if scenario.read_only_repositories:
            lines.extend(["**可搜索的只读 Git 仓库**", ""])
            lines.extend(
                (
                    f"- `{item.repository}` · Git `{item.revision}` · tree `{item.tree_sha1}`"
                    f"；依据：{'消息当时' if item.temporal_basis == 'source_time' else '评测时版本'}"
                )
                for item in scenario.read_only_repositories
            )
            lines.append("")
        if source.resources:
            lines.extend(["**原始附件与模型输入关系**", ""])
            for item in source.resources:
                label = f"- [{item.raw_local_path}]({item.raw_local_path}) · 源消息 `{item.source_message_id}` · SHA-256 `{item.raw_sha256}`"
                if item.excluded_from_model:
                    label += " · 原件未纳入模型输入"
                elif item.model_input_path == item.raw_local_path:
                    label += " · 此原件也会作为模型输入"
                elif item.model_input_path:
                    label += f" · 原件仅供审核，模型使用 `{item.model_input_path}`"
                else:
                    label += " · 原件仅供审核"
                lines.append(label)
            lines.append("")
        lines.extend(["**待审核的预期**", ""])
        lines.extend(f"- {item}" for item in case.expected.criteria)
        labels = {"required": "需要", "optional": "可选", "not_required": "不要求"}
        lines.extend(
            f"- 技能 `{item.name}`：{labels[item.requirement]}。{item.rationale}"
            for item in case.expected.skill_expectations
        )
        lines.append("")
        if case.expected.reference_reply:
            lines.extend(["**参考回复**", "", case.expected.reference_reply, ""])
        if case.audit.excluded_raw_image:
            lines.extend([f"- 图片边界：{case.audit.excluded_raw_image}", ""])
        if case.audit.excluded_quote:
            lines.extend([f"- 引用边界：{case.audit.excluded_quote}", ""])
    return preamble + "\n".join(lines)
