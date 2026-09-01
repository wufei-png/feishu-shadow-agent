from __future__ import annotations

import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from html import escape
from typing import Any, cast

from .approval_cards import build_approval_card
from .config import AppConfig
from .feishu.client import FeishuClient
from .ingestion import MessageNormalizer
from .jsonl import JSONLLogger
from .store.sqlite_store import SQLiteStore
from .time_utils import format_instant, utc_now
from .types import (
    ActionRecord,
    ActionStatus,
    DispatchAttemptStatus,
    DispatchErrorStage,
    LarkCliResult,
    NormalizedMessage,
)

EXPECTED_MENTION_RE = re.compile(
    r"<at\s+[^>]*user_id=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE
)
NOTIFICATION_AT_SPAN_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)
NOTIFICATION_AT_TAG_RE = re.compile(r"</?at\b[^>]*>", re.IGNORECASE)
NOTIFICATION_GROUP_MENTION_RE = re.compile(r"@所有人|@_?all\b", re.IGNORECASE)
READBACK_BLOCKING_WARNINGS = {
    "readback_reply_target_mismatch",
    "readback_reply_target_unavailable",
    "readback_mentions_mismatch",
    "readback_mentions_unavailable",
}


@dataclass(frozen=True)
class DispatchSummary:
    processed: int = 0
    sent: int = 0
    previewed: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class ReadbackOutcome:
    result: dict[str, Any] | None
    warnings: list[str]
    message: NormalizedMessage | None = None


class Dispatcher:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        feishu_client: FeishuClient,
        config: AppConfig,
        logger: JSONLLogger,
        interactive_cards_available: bool = False,
    ):
        self.store = store
        self.feishu = feishu_client
        self.config = config
        self.logger = logger
        self.interactive_cards_available = interactive_cards_available
        self.normalizer = MessageNormalizer(owner_open_id=config.owner.open_id)

    def dispatch(
        self,
        *,
        run_id: str,
        allow_send_reply_actual: bool,
        allow_owner_notification_actual: bool,
        blocked_send_reply_reason: str | None = None,
        limit: int = 50,
    ) -> DispatchSummary:
        summary = DispatchSummary()
        recovered = self.store.mark_stale_sending_actions_failed_needs_review()
        if recovered:
            self.logger.warning(
                "dispatch_stale_sending_recovered",
                run_id=run_id,
                data={"actions": recovered},
            )
            for recovery in recovered:
                if recovery.get("status") != ActionStatus.FAILED_NEEDS_REVIEW.value:
                    continue
                recovered_action = self.store.get_action(int(recovery["action_id"]))
                if recovered_action is not None:
                    self._queue_failed_reply_notification(
                        recovered_action,
                        status=ActionStatus.FAILED_NEEDS_REVIEW.value,
                        result=recovered_action.result,
                        run_id=run_id,
                        dedupe_suffix="stale_recovery",
                    )
        actions = self.store.list_dispatchable_actions(limit=limit)
        if allow_owner_notification_actual:
            seen_action_ids = {action.id for action in actions}
            actions.extend(
                action
                for action in self.store.list_dispatchable_actions(
                    limit=limit, kind="owner_notification"
                )
                if action.id not in seen_action_ids
            )
        self.logger.debug(
            "dispatch_actions_loaded",
            run_id=run_id,
            data={
                "count": len(actions),
                "limit": limit,
                "allow_send_reply_actual": allow_send_reply_actual,
                "allow_owner_notification_actual": allow_owner_notification_actual,
                "blocked_send_reply_reason": blocked_send_reply_reason,
            },
        )
        for action in actions:
            mode_allows_actual = action.execution_mode == "production"
            runtime_allows_actual = (
                allow_send_reply_actual
                if action.kind == "send_reply"
                else allow_owner_notification_actual
                if action.kind == "owner_notification"
                else False
            )
            actual_allowed = mode_allows_actual and runtime_allows_actual
            if actual_allowed:
                claim = self.store.claim_action_for_dispatch(action.id, run_id=run_id)
                if claim is None:
                    self.logger.warning(
                        "dispatch_action_claim_skipped",
                        run_id=run_id,
                        data={
                            "action_id": action.id,
                            "kind": action.kind,
                            "reason": "claim_failed",
                        },
                    )
                    summary = _bump(summary, skipped=1)
                    continue
                claimed = claim.action
                result, action_status = self._execute_actual(
                    claimed,
                    attempt_id=claim.attempt.id,
                    run_id=run_id,
                )
                finished = self.store.finish_claimed_action(
                    claimed.id,
                    attempt_id=claim.attempt.id,
                    status=action_status,
                    result=result,
                )
                if finished is None:
                    self.logger.warning(
                        "dispatch_action_finish_skipped",
                        run_id=run_id,
                        data={
                            "action_id": claimed.id,
                            "kind": claimed.kind,
                            "attempt_id": claim.attempt.id,
                            "attempted_status": action_status,
                            "reason": "claim_no_longer_current",
                        },
                    )
                    summary = _bump(summary, processed=1, skipped=1)
                    continue
                sent = action_status == ActionStatus.SENT.value
                self.logger.emit(
                    "info" if sent else "error",
                    "dispatch_action_completed",
                    run_id=run_id,
                    data={
                        "action_id": claimed.id,
                        "kind": claimed.kind,
                        "status": action_status,
                        "error_stage": result.get("error_stage"),
                        "warnings": result.get("warnings", []),
                    },
                )
                if result.get("warnings"):
                    self.logger.warning(
                        "dispatch_action_completed_with_warnings",
                        run_id=run_id,
                        data={
                            "action_id": claimed.id,
                            "kind": claimed.kind,
                            "warnings": result.get("warnings", []),
                        },
                    )
                if not sent:
                    self._queue_failed_reply_notification(
                        claimed,
                        status=action_status,
                        result=result,
                        run_id=run_id,
                        dedupe_suffix=f"attempt:{claim.attempt.id}",
                    )
                summary = _bump(
                    summary, processed=1, sent=1 if sent else 0, failed=0 if sent else 1
                )
                continue

            self.preview_action_record(
                action,
                run_id=run_id,
                blocked_actual_reason=(
                    f"execution_mode_{action.execution_mode}"
                    if not mode_allows_actual
                    else blocked_send_reply_reason
                    if action.kind == "send_reply"
                    else None
                ),
            )
            summary = _bump(summary, processed=1, previewed=1)
        return summary

    def _queue_failed_reply_notification(
        self,
        action: ActionRecord,
        *,
        status: str,
        result: dict[str, Any],
        run_id: str,
        dedupe_suffix: str,
    ) -> None:
        if action.kind != "send_reply":
            return
        uncertain = status == ActionStatus.FAILED_NEEDS_REVIEW.value
        stage = result.get("error_stage") or result.get("recovery_reason") or "unknown"
        stage_result = result.get(str(result.get("error_stage") or ""))
        error = (
            cast(dict[str, Any], stage_result).get("error")
            if isinstance(stage_result, dict)
            else None
        )
        if not error:
            warnings = result.get("warnings")
            error = (
                cast(list[Any], warnings)
                if isinstance(warnings, list) and warnings
                else None
            )
        task_id: int | str | None = action.task_id
        if action.task_id is not None:
            with suppress(KeyError):
                task_id = self.store.get_task_by_id(action.task_id).short_id
        payload: dict[str, Any] = {
            "type": "dispatch_uncertain" if uncertain else "dispatch_failed",
            "task_id": task_id,
            "reason": status,
            "message": (
                f"Reply dispatch action {action.id} is uncertain; inspect evidence "
                "before retrying."
                if uncertain
                else f"Reply dispatch action {action.id} failed before completion."
            ),
            "stage": stage,
            "target": action.target_message_id,
            "error": error,
            "dedupe_key": f"dispatch:{action.id}:{dedupe_suffix}",
        }
        try:
            notification_action_id = self.store.create_owner_notification_action(
                task_id=action.task_id,
                payload=payload,
                execution_mode=action.execution_mode,
            )
        except Exception as exc:  # noqa: BLE001
            # Notification persistence is isolated from the dispatch result;
            # retain evidence even when the store backend raises.
            self.logger.emit(
                "error",
                "dispatch_failure_notification_queue_failed",
                run_id=run_id,
                data={
                    "action_id": action.id,
                    "status": status,
                    "error": str(exc),
                },
            )
            return
        self.logger.warning(
            "dispatch_failure_notification_queued",
            run_id=run_id,
            data={
                "action_id": action.id,
                "notification_action_id": notification_action_id,
                "status": status,
            },
        )

    def preview_action(self, action_id: int, *, run_id: str) -> dict[str, Any] | None:
        action = self.store.get_action(action_id)
        if (
            action is None
            or action.status != "pending"
            or action.kind not in {"send_reply", "owner_notification"}
        ):
            return None
        return self.preview_action_record(action, run_id=run_id)

    def preview_action_record(
        self,
        action: ActionRecord,
        *,
        run_id: str,
        blocked_actual_reason: str | None = None,
    ) -> dict[str, Any]:
        result = self._execute_preview(action)
        if blocked_actual_reason:
            result["blocked_actual_reason"] = blocked_actual_reason
            result["warnings"].append(f"actual_send_blocked_{blocked_actual_reason}")
            self.logger.warning(
                "dispatch_actual_send_blocked",
                run_id=run_id,
                data={
                    "action_id": action.id,
                    "kind": action.kind,
                    "reason": blocked_actual_reason,
                },
            )
        self.store.record_action_preview(action.id, result)
        self.logger.emit(
            "info",
            "dispatch_action_previewed",
            run_id=run_id,
            data={
                "action_id": action.id,
                "kind": action.kind,
                "error_stage": result.get("error_stage"),
                "blocked_actual_reason": blocked_actual_reason,
            },
        )
        return {
            "action_id": action.id,
            "kind": action.kind,
            "task_id": action.task_id,
            "target_message_id": action.target_message_id,
            "result": result,
        }

    def mark_action_sent_after_readback(
        self,
        action_id: int,
        *,
        sent_message_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        action = self.store.get_action(action_id)
        if action is None:
            return {"status": "failed", "error": f"action not found: {action_id}"}
        if not sent_message_id:
            return {"status": "failed", "error": "sent_message_id is required"}
        result = _empty_result()
        result["sent_message_id"] = sent_message_id
        outcome = self._readback_outcome(
            action, sent_message_id=sent_message_id, run_id=run_id
        )
        result["readback"] = outcome.result
        result["warnings"].extend(outcome.warnings)
        evidence_error = _mark_sent_evidence_error(
            action,
            sent_message_id=sent_message_id,
            readback=result["readback"],
            warnings=result["warnings"],
        )
        if evidence_error is not None:
            return {
                "status": "failed",
                "error": evidence_error,
                "action_id": action_id,
                "result": result,
            }
        try:
            updated = self.store.mark_action_sent_after_evidence(
                action_id,
                sent_message_id=sent_message_id,
                result=result,
                run_id=run_id,
                readback_message=outcome.message,
                watch_until=_watch_until(self.config.lifecycle.watch_minutes)
                if outcome.message is not None
                else None,
            )
        except ValueError as exc:
            return {
                "status": "failed",
                "error": str(exc),
                "action_id": action_id,
                "result": result,
            }
        return {
            "status": "sent",
            "action_id": updated.id,
            "sent_message_id": sent_message_id,
            "result": updated.result,
        }

    def _execute_actual(
        self, action: ActionRecord, *, attempt_id: int, run_id: str
    ) -> tuple[dict[str, Any], str]:
        result = _empty_result()
        try:
            dry_run = self._dry_run(action)
        except Exception as exc:  # noqa: BLE001
            # Dry-run adapters are pluggable and may raise implementation-
            # specific errors; convert them to an action result.
            result["dry_run"] = _exception_command_result(action, "dry_run", exc)
            result["error_stage"] = "dry_run"
            self.store.update_dispatch_attempt(
                attempt_id,
                status=DispatchAttemptStatus.FAILED.value,
                dry_run_result=result["dry_run"],
                error_stage=DispatchErrorStage.DRY_RUN.value,
            )
            return result, ActionStatus.FAILED.value
        result["dry_run"] = _command_result(dry_run)
        if not dry_run.ok:
            result["error_stage"] = "dry_run"
            self.store.update_dispatch_attempt(
                attempt_id,
                status=DispatchAttemptStatus.FAILED.value,
                dry_run_result=result["dry_run"],
                error_stage=DispatchErrorStage.DRY_RUN.value,
            )
            return result, ActionStatus.FAILED.value
        self.store.update_dispatch_attempt(
            attempt_id,
            status=DispatchAttemptStatus.DRY_RUN_OK.value,
            dry_run_result=result["dry_run"],
        )

        try:
            send = self._send(action)
        except Exception as exc:  # noqa: BLE001
            # Sending is an external boundary; preserve uncertain-send
            # semantics for every adapter failure.
            result["send"] = _exception_command_result(action, "send", exc)
            result["error_stage"] = "send"
            self.store.update_dispatch_attempt(
                attempt_id,
                status=DispatchAttemptStatus.UNCERTAIN.value,
                send_result=result["send"],
                error_stage=DispatchErrorStage.SEND.value,
            )
            return result, ActionStatus.FAILED_NEEDS_REVIEW.value
        result["send"] = _command_result(send)
        if not send.ok:
            result["error_stage"] = "send"
            attempt_status = _send_failure_attempt_status(send)
            action_status = (
                ActionStatus.FAILED_NEEDS_REVIEW.value
                if attempt_status == DispatchAttemptStatus.UNCERTAIN.value
                else ActionStatus.FAILED.value
            )
            self.store.update_dispatch_attempt(
                attempt_id,
                status=attempt_status,
                send_result=result["send"],
                error_stage=DispatchErrorStage.SEND.value,
            )
            return result, action_status

        target_message_id = _target_message_id(action)
        sent_message_id = _extract_message_id(send.json_data, exclude=target_message_id)
        if sent_message_id is None:
            result["warnings"].append("sent_message_id_missing")
            result["error_stage"] = "send"
            self.store.update_dispatch_attempt(
                attempt_id,
                status=DispatchAttemptStatus.UNCERTAIN.value,
                send_result=result["send"],
                error_stage=DispatchErrorStage.SEND.value,
            )
            return result, ActionStatus.FAILED_NEEDS_REVIEW.value
        else:
            result["sent_message_id"] = sent_message_id
        self.store.update_dispatch_attempt(
            attempt_id,
            status=DispatchAttemptStatus.SEND_OK.value,
            send_result=result["send"],
            sent_message_id=sent_message_id,
        )
        try:
            readback = self._readback(
                action, sent_message_id=sent_message_id, run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001
            # Readback is best-effort evidence and must not abort dispatch
            # bookkeeping when a client adapter raises.
            result["readback"] = _exception_readback_result(sent_message_id, exc)
            result["warnings"].append("readback_exception")
            self.logger.warning(
                "dispatch_readback_exception",
                run_id=run_id,
                data={
                    "action_id": action.id,
                    "kind": action.kind,
                    "sent_message_id": sent_message_id,
                    "error": str(exc),
                },
            )
        else:
            result["readback"] = readback["result"]
            result["warnings"].extend(readback["warnings"])
        # A successful send with a returned message ID makes the action terminal.
        # Readback grades evidence and records warnings, but a missing or
        # incomplete readback must not trigger an automatic resend.
        readback_ok = _readback_attempt_verified(
            action, readback=result["readback"], warnings=result["warnings"]
        )
        self.store.update_dispatch_attempt(
            attempt_id,
            status=DispatchAttemptStatus.READBACK_OK.value
            if readback_ok
            else DispatchAttemptStatus.SEND_OK.value,
            readback_result=cast(dict[str, Any], result["readback"])
            if isinstance(result["readback"], dict)
            else None,
            sent_message_id=sent_message_id,
            error_stage=None if readback_ok else DispatchErrorStage.READBACK.value,
            finish=True,
        )
        return result, ActionStatus.SENT.value

    def _execute_preview(self, action: ActionRecord) -> dict[str, Any]:
        result = _empty_result()
        try:
            dry_run = self._dry_run(action)
        except Exception as exc:  # noqa: BLE001
            # Preview adapters are isolated per action and normalized into the
            # structured preview result.
            result["dry_run"] = _exception_command_result(action, "dry_run", exc)
            result["error_stage"] = "dry_run"
            return result
        result["dry_run"] = _command_result(dry_run)
        if not dry_run.ok:
            result["error_stage"] = "dry_run"
        return result

    def _dry_run(self, action: ActionRecord) -> LarkCliResult:
        return self._call_send(action, dry_run=True)

    def _send(self, action: ActionRecord) -> LarkCliResult:
        return self._call_send(action, dry_run=False)

    def _call_send(self, action: ActionRecord, *, dry_run: bool) -> LarkCliResult:
        if action.kind == "send_reply":
            target_message_id = _target_message_id(action)
            text = _payload_text(action)
            identity = _payload_identity(action)
            if not target_message_id:
                return _local_error(action, "send_reply target_message_id is missing")
            if not text:
                return _local_error(action, "send_reply text is missing")
            if identity not in {"user", "bot"}:
                return _local_error(action, "send_reply identity must be user or bot")
            return self.feishu.reply_message(
                as_identity=identity,
                message_id=target_message_id,
                text=text,
                idempotency_key=action.idempotency_key,
                dry_run=dry_run,
            )
        if action.kind == "owner_notification":
            if (
                self.interactive_cards_available
                and action.payload.get("type") == "approval_required"
            ):
                try:
                    card = build_approval_card(action.payload)
                except ValueError as exc:
                    return _local_error(action, str(exc))
                return self.feishu.owner_card(
                    owner_open_id=self.config.owner.open_id,
                    card=card,
                    idempotency_key=action.idempotency_key,
                    dry_run=dry_run,
                )
            text = _owner_notification_text(action.payload)
            return self.feishu.owner_message(
                owner_open_id=self.config.owner.open_id,
                text=text,
                idempotency_key=action.idempotency_key,
                dry_run=dry_run,
            )
        return _local_error(action, f"unsupported action kind: {action.kind}")

    def _readback(
        self, action: ActionRecord, *, sent_message_id: str | None, run_id: str
    ) -> dict[str, Any]:
        outcome = self._readback_outcome(
            action, sent_message_id=sent_message_id, run_id=run_id
        )
        if outcome.message is not None:
            result = dict(outcome.result or {})
            result["inserted"] = self._persist_readback_message(action, outcome.message)
            return {"result": result, "warnings": outcome.warnings}
        return {"result": outcome.result, "warnings": outcome.warnings}

    def _readback_outcome(
        self,
        action: ActionRecord,
        *,
        sent_message_id: str | None,
        run_id: str,
    ) -> ReadbackOutcome:
        if sent_message_id is None:
            self.logger.warning(
                "dispatch_readback_skipped",
                run_id=run_id,
                data={
                    "action_id": action.id,
                    "kind": action.kind,
                    "reason": "sent_message_id_missing",
                },
            )
            return ReadbackOutcome(
                result=None, warnings=["readback_skipped_no_sent_message_id"]
            )
        identity = _payload_identity(action) if action.kind == "send_reply" else "bot"
        warnings: list[str] = []
        try:
            page = self.feishu.get_messages(
                as_identity=identity, message_ids=[sent_message_id]
            )
        except Exception as exc:  # noqa: BLE001
            # Readback warning collection is intentionally non-fatal to the
            # dispatch loop.
            self.logger.warning(
                "dispatch_readback_failed",
                run_id=run_id,
                data={
                    "action_id": action.id,
                    "kind": action.kind,
                    "sent_message_id": sent_message_id,
                    "error": str(exc),
                },
            )
            return ReadbackOutcome(
                result={"ok": False, "error": str(exc), "message_id": sent_message_id},
                warnings=["readback_failed"],
            )
        item = _find_message(page.items, sent_message_id)
        if item is None:
            self.logger.warning(
                "dispatch_readback_message_missing",
                run_id=run_id,
                data={
                    "action_id": action.id,
                    "kind": action.kind,
                    "sent_message_id": sent_message_id,
                },
            )
            return ReadbackOutcome(
                result={"ok": False, "message_id": sent_message_id, "raw": page.raw},
                warnings=["readback_message_missing"],
            )
        raw = dict(item)
        raw["sent_by_agent"] = True
        message = self.normalizer.normalize(raw)
        if action.kind == "send_reply":
            target = _target_message_id(action)
            if (
                message.reply_to_message_id
                and target
                and message.reply_to_message_id != target
            ):
                warnings.append("readback_reply_target_mismatch")
            elif not message.reply_to_message_id:
                warnings.append("readback_reply_target_unavailable")
            expected_mentions = _expected_mentions(_payload_text(action))
            if expected_mentions and not message.mentions:
                warnings.append("readback_mentions_unavailable")
            elif expected_mentions and not expected_mentions <= set(message.mentions):
                warnings.append("readback_mentions_mismatch")
        return ReadbackOutcome(
            result={
                "ok": True,
                "message_id": sent_message_id,
                "inserted": False,
                "reply_to_message_id": message.reply_to_message_id,
                "mentions": message.mentions,
                "text": message.text,
                "raw": page.raw,
            },
            warnings=warnings,
            message=message,
        )

    def _persist_readback_message(
        self, action: ActionRecord, message: NormalizedMessage
    ) -> bool:
        inserted = self.store.upsert_message(message)
        if action.kind == "send_reply" and action.task_id is not None:
            self.store.record_agent_message_for_task(
                action.task_id,
                message,
                watch_until=_watch_until(self.config.lifecycle.watch_minutes),
            )
        return inserted


def _empty_result() -> dict[str, Any]:
    return {
        "dry_run": None,
        "send": None,
        "sent_message_id": None,
        "readback": None,
        "warnings": [],
        "error_stage": None,
    }


def _command_result(result: LarkCliResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "argv": result.argv,
        "exit_code": result.exit_code,
        "json": result.json_data,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "error": result.error,
        "timed_out": result.timed_out,
    }


def _exception_command_result(
    action: ActionRecord, stage: str, exc: Exception
) -> dict[str, Any]:
    return {
        "ok": False,
        "argv": ["dispatch", action.kind, str(action.id), stage],
        "exit_code": None,
        "json": None,
        "stdout": "",
        "stderr": "",
        "error": str(exc),
        "exception_type": type(exc).__name__,
        "timed_out": False,
    }


def _exception_readback_result(
    sent_message_id: str | None, exc: Exception
) -> dict[str, Any]:
    return {
        "ok": False,
        "message_id": sent_message_id,
        "error": str(exc),
        "exception_type": type(exc).__name__,
    }


def _send_failure_attempt_status(result: LarkCliResult) -> str:
    if _send_failure_proves_not_sent(result):
        return DispatchAttemptStatus.FAILED.value
    return DispatchAttemptStatus.UNCERTAIN.value


def _send_failure_proves_not_sent(result: LarkCliResult) -> bool:
    if result.argv[:1] == ["dispatch"]:
        return True
    if not isinstance(result.error, str):
        return False
    error = result.error.strip().lower()
    return error == "send rejected" or error.startswith(
        ("api rejected", "feishu api rejected")
    )


def _mark_sent_evidence_error(
    action: ActionRecord,
    *,
    sent_message_id: str,
    readback: Any,
    warnings: list[str],
) -> str | None:
    if not isinstance(readback, dict):
        return "readback did not verify sent message"
    readback_map = cast(dict[str, Any], readback)
    if readback_map.get("ok") is not True:
        return "readback did not verify sent message"
    if readback_map.get("message_id") != sent_message_id:
        return "readback message_id did not match sent_message_id"
    if action.kind != "send_reply":
        return _mark_sent_text_evidence_error(
            expected=_owner_notification_text(action.payload), readback=readback_map
        )
    target_message_id = _target_message_id(action)
    if (
        not target_message_id
        or readback_map.get("reply_to_message_id") != target_message_id
    ):
        return "readback reply_to_message_id did not match action target"
    blocking_warnings = {"readback_mentions_mismatch", "readback_mentions_unavailable"}
    if blocking_warnings & set(warnings):
        return "readback mentions did not match action text"
    text_error = _mark_sent_text_evidence_error(
        expected=_payload_text(action), readback=readback_map
    )
    if text_error is not None:
        return text_error
    return None


def _mark_sent_text_evidence_error(
    *, expected: str, readback: dict[str, Any]
) -> str | None:
    expected_text = _evidence_text(expected)
    text = readback.get("text")
    actual_text = _evidence_text(text if isinstance(text, str) else "")
    if expected_text and actual_text != expected_text:
        return "readback text did not match action payload"
    return None


def _evidence_text(value: str) -> str:
    return " ".join(EXPECTED_MENTION_RE.sub("", value).split())


def _readback_attempt_verified(
    action: ActionRecord, *, readback: Any, warnings: list[str]
) -> bool:
    if not isinstance(readback, dict):
        return False
    readback_map = cast(dict[str, Any], readback)
    if readback_map.get("ok") is not True:
        return False
    return not (
        action.kind == "send_reply" and READBACK_BLOCKING_WARNINGS & set(warnings)
    )


def _local_error(action: ActionRecord, message: str) -> LarkCliResult:
    return LarkCliResult(
        argv=["dispatch", action.kind, str(action.id)], exit_code=None, error=message
    )


def _target_message_id(action: ActionRecord) -> str | None:
    value = (
        action.target_message_id
        or action.payload.get("reply_target_message_id")
        or action.payload.get("target_message_id")
    )
    return value if isinstance(value, str) and value else None


def _payload_text(action: ActionRecord) -> str:
    value = action.payload.get("text") or action.payload.get("composed_text") or ""
    return value if isinstance(value, str) else ""


def _payload_identity(action: ActionRecord) -> str:
    value = action.payload.get("identity") or "user"
    return value if isinstance(value, str) else "user"


def _owner_notification_text(payload: dict[str, Any]) -> str:
    lines = ["[feishu-shadow-agent] owner notification"]
    for key in ("type", "task_id", "approval_id", "reason", "message"):
        value = payload.get(key)
        if value:
            lines.append(f"{key}: {_notification_display_text(str(value))}")
    source = _owner_notification_source(payload.get("source"))
    if source:
        lines.append(f"source: {source}")
    incoming = _owner_notification_incoming_message(payload.get("incoming_message"))
    if incoming:
        lines.extend(incoming)
    if "suggested_reply" in payload:
        suggested = payload.get("suggested_reply")
        if isinstance(suggested, str) and suggested.strip():
            lines.append(f"suggested_reply: {_compact_notification_text(suggested)}")
        else:
            lines.append("suggested_reply: <none>")
    if "approvable" in payload:
        lines.append(f"approvable: {'yes' if payload.get('approvable') else 'no'}")
    for key in (
        "stage",
        "target",
        "reply_target_message_id",
        "attempt_count",
        "pending_approval_ids",
        "statuses",
        "error",
    ):
        detail = _owner_notification_detail(payload.get(key))
        if detail:
            lines.append(f"{key}: {detail}")
    pending_approvals = _owner_notification_pending_approvals(
        payload.get("pending_approvals")
    )
    if pending_approvals:
        lines.append("pending_approvals:")
        lines.extend(pending_approvals)
    preview = payload.get("preview")
    if isinstance(preview, str) and preview:
        lines.append(f"preview: {_compact_notification_text(preview)}")
    commands = payload.get("commands")
    if isinstance(commands, list) and commands:
        lines.append("commands:")
        lines.extend(str(command) for command in cast(list[Any], commands))
    return "\n".join(lines)


def _owner_notification_source(value: Any) -> str:
    if isinstance(value, str):
        return _notification_display_text(value)
    if not isinstance(value, dict):
        return ""
    value_map = cast(dict[str, Any], value)
    chat = " ".join(
        _notification_display_text(str(part))
        for part in (value_map.get("chat_type"), value_map.get("chat_id"))
        if part
    )
    sender = value_map.get("sender_name") or value_map.get("sender_id")
    task_label = value_map.get("task_label")
    parts = [
        _notification_display_text(str(part))
        for part in (chat, sender, task_label)
        if part
    ]
    return " / ".join(parts)


def _owner_notification_incoming_message(value: Any) -> list[str]:
    if isinstance(value, str):
        return (
            [f"incoming: {_compact_notification_text(value)}"] if value.strip() else []
        )
    if not isinstance(value, dict):
        return []
    value_map = cast(dict[str, Any], value)
    lines: list[str] = []
    message_id = value_map.get("message_id")
    if message_id:
        lines.append(f"message_id: {message_id}")
    text = value_map.get("text")
    if isinstance(text, str) and text.strip():
        lines.append(f"incoming: {_compact_notification_text(text)}")
    message_app_link = value_map.get("message_app_link")
    if isinstance(message_app_link, str) and message_app_link.startswith(
        "https://applink.feishu.cn/"
    ):
        lines.append(f"message_link: {_notification_display_text(message_app_link)}")
    return lines


def _owner_notification_detail(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if isinstance(value, list):
        return ", ".join(
            _notification_display_text(str(item)) for item in cast(list[Any], value)
        )
    if isinstance(value, dict):
        return _compact_notification_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        )
    return _compact_notification_text(str(value))


def _owner_notification_pending_approvals(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    lines: list[str] = []
    for item in cast(list[Any], value):
        if not isinstance(item, dict):
            continue
        item_map = cast(dict[str, Any], item)
        approval_id = _notification_display_text(
            str(item_map.get("approval_id") or "unknown")
        )
        kind = _notification_display_text(str(item_map.get("kind") or "unknown"))
        reason = _notification_display_text(str(item_map.get("reason") or ""))
        preview = item_map.get("preview")
        parts = [approval_id, kind]
        if reason:
            parts.append(f"reason: {reason}")
        if isinstance(preview, str) and preview.strip():
            parts.append(f"preview: {_compact_notification_text(preview)}")
        commands = item_map.get("commands")
        if isinstance(commands, list) and commands:
            parts.append(
                f"commands: {', '.join(str(command) for command in cast(list[Any], commands))}"
            )
        lines.append("- " + " | ".join(parts))
    return lines


def _compact_notification_text(value: str, *, limit: int = 500) -> str:
    compact = _notification_display_text(value)
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


def _notification_display_text(value: str) -> str:
    neutralized = NOTIFICATION_AT_SPAN_RE.sub(
        lambda match: _neutralize_at_token(match.group(0)), value
    )
    neutralized = NOTIFICATION_AT_TAG_RE.sub(
        lambda match: _neutralize_at_token(match.group(0)), neutralized
    )
    neutralized = NOTIFICATION_GROUP_MENTION_RE.sub(
        lambda match: "＠" + match.group(0)[1:],
        neutralized,
    )
    return " ".join(escape(neutralized, quote=False).split())


def _neutralize_at_token(value: str) -> str:
    return value.replace("<", "‹").replace(">", "›")


def _extract_message_id(value: Any, *, exclude: str | None = None) -> str | None:
    for key in ("sent_message_id", "sentMessageId", "message_id", "messageId"):
        found = _find_key(value, key)
        if isinstance(found, str) and found and found != exclude:
            return found
    return None


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        value_map = cast(dict[str, Any], value)
        if key in value_map:
            return value_map[key]
        for child in value_map.values():
            found = _find_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in cast(list[Any], value):
            found = _find_key(child, key)
            if found is not None:
                return found
    return None


def _find_message(
    items: list[dict[str, Any]], message_id: str
) -> dict[str, Any] | None:
    for item in items:
        found = item.get("message_id") or item.get("messageId") or item.get("id")
        if found == message_id:
            return item
    if len(items) == 1:
        return items[0]
    return None


def _expected_mentions(text: str) -> set[str]:
    return set(EXPECTED_MENTION_RE.findall(text))


def _watch_until(watch_minutes: int) -> str:
    return format_instant(utc_now() + timedelta(minutes=watch_minutes))


def _bump(
    summary: DispatchSummary,
    *,
    processed: int = 0,
    sent: int = 0,
    previewed: int = 0,
    failed: int = 0,
    skipped: int = 0,
) -> DispatchSummary:
    return DispatchSummary(
        processed=summary.processed + processed,
        sent=summary.sent + sent,
        previewed=summary.previewed + previewed,
        failed=summary.failed + failed,
        skipped=summary.skipped + skipped,
    )
