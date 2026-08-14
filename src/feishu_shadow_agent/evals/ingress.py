from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, cast

from pydantic import ValidationError

from ..agent_backend import AgentBackend
from ..ingestion import MessageNormalizer, normalize_message_sent_at
from ..message_eligibility import MessageEligibilityPolicy
from ..types import NormalizedMessage
from .artifacts import EvalError, message_id_from_raw, text_excerpt
from .schemas import IngressJudgeOutput, IngressReviewLabels

DECISIONS = {"kept", "dropped"}


class GroupIngressEvaluator:
    def __init__(self, *, owner_open_id: str):
        self.normalizer = MessageNormalizer(owner_open_id=owner_open_id)
        self.policy = MessageEligibilityPolicy()

    def build_timeline(
        self,
        raw_messages: list[dict[str, Any]],
        *,
        sources_by_message: Mapping[str, Iterable[str]],
        owner: dict[str, Any],
        chat: dict[str, Any],
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for index, raw in enumerate(sorted(raw_messages, key=_raw_sort_key), start=1):
            message = self.normalizer.normalize(raw, default_chat_type="group")
            if message.chat_type != "group":
                raise EvalError(
                    f"ingress timeline message is not a group message: {message.message_id}"
                )
            sources = sorted(set(sources_by_message.get(message.message_id, [])))
            decision = self.policy.decide(message, sources=sources)
            rows.append(
                _timeline_row(message, index=index, sources=sources, decision=decision)
            )
        return {
            "schema_version": "ingress_timeline_v1",
            "instruction": {
                "task": "judge_ingress_filter",
                "production_ingress_contract": "production_v1",
            },
            "owner": owner,
            "chat": chat,
            "messages": rows,
        }


def active_watch_sources(
    raw_messages: list[dict[str, Any]],
    *,
    owner_open_id: str,
    active_tasks: dict[str, Any],
) -> set[str]:
    normalizer = MessageNormalizer(owner_open_id=owner_open_id)
    matched: set[str] = set()
    for raw in raw_messages:
        message = normalizer.normalize(raw, default_chat_type="group")
        if _matches_active_watch_fixture(message, active_tasks.values()):
            matched.add(message.message_id)
    return matched


def build_review_labels(
    timeline: dict[str, Any],
    *,
    judge_labels: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    labels: list[dict[str, Any]] = []
    judge_labels = judge_labels or {}
    for message in timeline_messages(timeline):
        message_id = str(message["message_id"])
        judge = judge_labels.get(message_id, {})
        expected = judge.get("expected_decision") or message["current_decision"]
        reason = judge.get("review_reason") or ""
        if expected not in DECISIONS:
            expected = message["current_decision"]
            reason = ""
        if expected == message["current_decision"]:
            reason = ""
        labels.append(
            {
                "message_id": message_id,
                "timeline_index": message["index"],
                "sent_at": message.get("sent_at"),
                "sender_name": message.get("sender_name"),
                "text_excerpt": text_excerpt(str(message.get("text") or "")),
                "current_decision": message["current_decision"],
                "reason_code": message["reason_code"],
                "expected_decision": expected,
                "review_reason": reason,
            }
        )
    labels.sort(
        key=lambda item: (
            item["expected_decision"] == item["current_decision"],
            item["timeline_index"],
        )
    )
    return {
        "schema_version": "ingress_review_labels_v1",
        "source_run": timeline.get("run_id"),
        "labels": labels,
    }


def run_ingress_judge(
    *,
    backend: AgentBackend,
    timeline: dict[str, Any],
    cwd: str | None = None,
) -> tuple[dict[str, dict[str, str]], str | None]:
    prompt = build_ingress_judge_prompt(timeline)
    try:
        result = backend.structured_output(
            prompt, output_model=IngressJudgeOutput, session_id=None, cwd=cwd
        )
    except Exception as exc:  # noqa: BLE001
        return {}, f"judge call raised {type(exc).__name__}: {exc}"
    if not result.ok:
        return {}, result.error or result.stderr or result.stdout or "judge failed"
    data: Any = result.json_data
    if data is None and result.stdout:
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return {}, f"judge stdout was not valid JSON: {exc}"
    try:
        output = IngressJudgeOutput.model_validate(data)
    except ValidationError as exc:
        return {}, f"judge output schema was invalid: {exc}"
    timeline_ids = [str(row["message_id"]) for row in timeline_messages(timeline)]
    output_ids = [item.message_id for item in output.labels]
    if len(output_ids) != len(set(output_ids)):
        return {}, "judge output contained duplicate message_id values"
    if set(output_ids) != set(timeline_ids):
        return {}, "judge output did not cover the full ingress timeline"
    return {
        item.message_id: {
            "expected_decision": item.expected_decision,
            "review_reason": item.review_reason,
        }
        for item in output.labels
    }, None


def build_ingress_judge_prompt(timeline: dict[str, Any]) -> str:
    payload = {
        "instruction": (
            "Independently judge whether every Feishu group message should be kept for owner task handling. "
            "Use the full timeline for context; do not mechanically accept current_decision and do not assign "
            "messages to tasks. Return exactly one label for every message. review_reason must be non-empty only "
            "when expected_decision differs from current_decision. Return strict JSON matching output_schema."
        ),
        "output_schema": IngressJudgeOutput.model_json_schema(),
        "timeline": timeline,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def validate_ingress_review_labels(
    *, timeline: dict[str, Any], labels: dict[str, Any]
) -> list[dict[str, Any]]:
    try:
        review = IngressReviewLabels.model_validate(labels)
    except ValidationError as exc:
        raise EvalError(f"invalid ingress review labels: {exc}") from exc
    rows = cast(list[dict[str, Any]], review.model_dump(mode="json")["labels"])
    messages = timeline_messages(timeline)
    if review.source_run != timeline.get("run_id"):
        raise EvalError("ingress review source_run does not match timeline run_id")
    expected_ids = [str(message["message_id"]) for message in messages]
    expected_set = set(expected_ids)
    messages_by_id = {str(message["message_id"]): message for message in messages}
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        message_id = str(row.get("message_id") or "")
        if message_id not in expected_set:
            raise EvalError(f"label message_id not found in timeline: {message_id}")
        if message_id in seen:
            raise EvalError(f"duplicate label message_id: {message_id}")
        seen.add(message_id)
        timeline_message = messages_by_id[message_id]
        for field, timeline_field in (
            ("timeline_index", "index"),
            ("current_decision", "current_decision"),
            ("reason_code", "reason_code"),
        ):
            if row[field] != timeline_message.get(timeline_field):
                raise EvalError(
                    f"{field} does not match ingress timeline for {message_id}"
                )
        expected_decision = str(row.get("expected_decision") or "")
        if expected_decision not in DECISIONS:
            raise EvalError(
                f"expected_decision must be kept or dropped for {message_id}"
            )
        current_decision = _current_decision_by_id(timeline, message_id)
        review_reason = str(row.get("review_reason") or "")
        if expected_decision != current_decision and not review_reason.strip():
            raise EvalError(
                f"review_reason is required when expected differs for {message_id}"
            )
        if expected_decision == current_decision and review_reason:
            raise EvalError(
                f"review_reason must be empty when expected matches for {message_id}"
            )
        normalized.append(
            {
                "message_id": message_id,
                "expected_decision": expected_decision,
                "review_reason": review_reason,
            }
        )
    missing = [message_id for message_id in expected_ids if message_id not in seen]
    if missing:
        raise EvalError(f"labels missing timeline messages: {', '.join(missing[:5])}")
    return normalized


def compare_ingress_golden(
    *, timeline: dict[str, Any], labels: dict[str, Any]
) -> dict[str, Any]:
    label_rows = labels.get("labels")
    if not isinstance(label_rows, list):
        raise EvalError("labels.yaml must contain labels list")
    label_rows = cast(list[Any], label_rows)
    label_maps = [
        cast(dict[str, Any], row) for row in label_rows if isinstance(row, dict)
    ]
    label_ids = [
        str(row.get("message_id")) for row in label_maps if row.get("message_id")
    ]
    if len(label_ids) != len(label_rows) or len(label_ids) != len(set(label_ids)):
        raise EvalError("golden labels must contain each message_id exactly once")
    expected_by_id: dict[str, dict[str, Any]] = {
        str(row.get("message_id")): row for row in label_maps if row.get("message_id")
    }
    messages = timeline_messages(timeline)
    if set(expected_by_id) != {str(message["message_id"]) for message in messages}:
        raise EvalError("golden labels must exactly cover ingress timeline")
    mismatches: list[dict[str, Any]] = []
    true_positive = true_negative = false_positive = false_negative = 0
    for message in messages:
        message_id = str(message["message_id"])
        label = expected_by_id[message_id]
        expected = str(label.get("expected_decision") or "")
        if expected not in DECISIONS:
            raise EvalError(f"invalid expected_decision for {message_id}")
        actual = str(message["current_decision"])
        if expected == "kept" and actual == "kept":
            true_positive += 1
        elif expected == "dropped" and actual == "dropped":
            true_negative += 1
        elif expected == "dropped" and actual == "kept":
            false_positive += 1
        elif expected == "kept" and actual == "dropped":
            false_negative += 1
        if expected != actual:
            mismatches.append(
                {
                    "message_id": message_id,
                    "expected_decision": expected,
                    "actual_decision": actual,
                    "error_type": "false_negative"
                    if expected == "kept"
                    else "false_positive",
                    "reason_code": message["reason_code"],
                    "review_reason": label.get("review_reason", ""),
                }
            )
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    return {
        "summary": {
            "total_messages": len(messages),
            "expected_kept": sum(
                1
                for row in expected_by_id.values()
                if row["expected_decision"] == "kept"
            ),
            "actual_kept": sum(
                1 for message in messages if message["current_decision"] == "kept"
            ),
            "true_positive": true_positive,
            "true_negative": true_negative,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": None
            if precision_denominator == 0
            else round(true_positive / precision_denominator, 3),
            "recall": None
            if recall_denominator == 0
            else round(true_positive / recall_denominator, 3),
            "passed": not mismatches,
        },
        "mismatches": mismatches,
    }


def timeline_messages(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    messages = timeline.get("messages")
    if not isinstance(messages, list) or any(
        not isinstance(item, dict) for item in cast(list[Any], messages)
    ):
        raise EvalError("ingress_timeline.yaml must contain only message mappings")
    message_rows = cast(list[dict[str, Any]], messages)
    message_ids = [str(item.get("message_id") or "") for item in message_rows]
    if not all(message_ids):
        raise EvalError("ingress timeline contains a message without message_id")
    if len(message_ids) != len(set(message_ids)):
        raise EvalError("ingress timeline contains duplicate message_id values")
    return message_rows


def _timeline_row(
    message: NormalizedMessage,
    *,
    index: int,
    sources: list[str],
    decision: Any,
) -> dict[str, Any]:
    return {
        "index": index,
        "message_id": message.message_id,
        "sent_at": message.sent_at,
        "sender_role": message.sender_role,
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "text": message.text,
        "mentions_owner": message.direct_mention,
        "at_all": message.at_all,
        "reply_to_message_id": message.reply_to_message_id,
        "thread_id": message.thread_id,
        "sources": sources,
        "current_decision": decision.decision,
        "reason_code": decision.reason_code,
    }


def _matches_active_watch_fixture(
    message: NormalizedMessage, fixtures: Iterable[Any]
) -> bool:
    if not message.chat_id:
        return False
    keys: set[str] = set()
    if message.reply_to_message_id:
        keys.add(f"msg:{message.reply_to_message_id}")
    if message.thread_id:
        keys.add(f"thread:{message.thread_id}")
    if message.sender_id:
        keys.add(f"user:{message.sender_id}")
    for fixture in fixtures:
        if fixture.chat_id != message.chat_id:
            continue
        if fixture.thread_id and fixture.thread_id != message.thread_id:
            continue
        if keys.intersection(fixture.watch_keys):
            return True
    return False


def _current_decision_by_id(timeline: dict[str, Any], message_id: str) -> str:
    for message in timeline_messages(timeline):
        if str(message.get("message_id")) == message_id:
            return str(message.get("current_decision") or "")
    return ""


def _raw_sort_key(raw: dict[str, Any]) -> tuple[str, str]:
    raw_time = _first_string(raw, "create_time", "created_at", "sent_at", "timestamp")
    return (
        normalize_message_sent_at(raw_time) or "",
        message_id_from_raw(raw),
    )


def _first_string(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None
