from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import AppConfig
from .feishu.client import FeishuClient
from .ingestion import MessageNormalizer
from .jsonl import JSONLLogger
from .store.sqlite_store import SQLiteStore
from .types import ActionRecord, LarkCliResult

WATCH_EXTEND_MINUTES = 120
EXPECTED_MENTION_RE = re.compile(r"<at\s+[^>]*user_id=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class DispatchSummary:
    processed: int = 0
    sent: int = 0
    previewed: int = 0
    failed: int = 0
    skipped: int = 0


class Dispatcher:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        feishu_client: FeishuClient,
        config: AppConfig,
        logger: JSONLLogger,
    ):
        self.store = store
        self.feishu = feishu_client
        self.config = config
        self.logger = logger
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
        actions = self.store.list_dispatchable_actions(limit=limit)
        if allow_owner_notification_actual:
            seen_action_ids = {action.id for action in actions}
            actions.extend(
                action
                for action in self.store.list_dispatchable_actions(limit=limit, kind="owner_notification")
                if action.id not in seen_action_ids
            )
        for action in actions:
            actual_allowed = (
                allow_send_reply_actual
                if action.kind == "send_reply"
                else allow_owner_notification_actual
                if action.kind == "owner_notification"
                else False
            )
            if actual_allowed:
                claimed = self.store.claim_action_for_dispatch(action.id)
                if claimed is None:
                    summary = _bump(summary, skipped=1)
                    continue
                result, sent = self._execute_actual(claimed, run_id=run_id)
                self.store.finish_action(
                    claimed.id,
                    status="sent" if sent else "failed",
                    result=result,
                )
                self.logger.emit(
                    "info" if sent else "error",
                    "dispatch_action_completed",
                    run_id=run_id,
                    data={"action_id": claimed.id, "kind": claimed.kind, "status": "sent" if sent else "failed"},
                )
                summary = _bump(summary, processed=1, sent=1 if sent else 0, failed=0 if sent else 1)
                continue

            self.preview_action_record(
                action,
                run_id=run_id,
                blocked_actual_reason=blocked_send_reply_reason if action.kind == "send_reply" else None,
            )
            summary = _bump(summary, processed=1, previewed=1)
        return summary

    def preview_action(self, action_id: int, *, run_id: str) -> dict[str, Any] | None:
        action = self.store.get_action(action_id)
        if action is None or action.status != "pending" or action.kind not in {"send_reply", "owner_notification"}:
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

    def _execute_actual(self, action: ActionRecord, *, run_id: str) -> tuple[dict[str, Any], bool]:
        result = _empty_result()
        try:
            dry_run = self._dry_run(action)
        except Exception as exc:
            result["dry_run"] = _exception_command_result(action, "dry_run", exc)
            result["error_stage"] = "dry_run"
            return result, False
        result["dry_run"] = _command_result(dry_run)
        if not dry_run.ok:
            result["error_stage"] = "dry_run"
            return result, False

        try:
            send = self._send(action)
        except Exception as exc:
            result["send"] = _exception_command_result(action, "send", exc)
            result["error_stage"] = "send"
            return result, False
        result["send"] = _command_result(send)
        if not send.ok:
            result["error_stage"] = "send"
            return result, False

        target_message_id = _target_message_id(action)
        sent_message_id = _extract_message_id(send.json_data, exclude=target_message_id)
        if sent_message_id is None:
            result["warnings"].append("sent_message_id_missing")
        else:
            result["sent_message_id"] = sent_message_id
        try:
            readback = self._readback(action, sent_message_id=sent_message_id, run_id=run_id)
        except Exception as exc:
            result["readback"] = _exception_readback_result(sent_message_id, exc)
            result["warnings"].append("readback_exception")
        else:
            result["readback"] = readback["result"]
            result["warnings"].extend(readback["warnings"])
        return result, True

    def _execute_preview(self, action: ActionRecord) -> dict[str, Any]:
        result = _empty_result()
        try:
            dry_run = self._dry_run(action)
        except Exception as exc:
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
            text = _owner_notification_text(action.payload)
            return self.feishu.owner_message(
                owner_open_id=self.config.owner.open_id,
                text=text,
                idempotency_key=action.idempotency_key,
                dry_run=dry_run,
            )
        return _local_error(action, f"unsupported action kind: {action.kind}")

    def _readback(self, action: ActionRecord, *, sent_message_id: str | None, run_id: str) -> dict[str, Any]:
        if sent_message_id is None:
            return {"result": None, "warnings": ["readback_skipped_no_sent_message_id"]}
        identity = _payload_identity(action) if action.kind == "send_reply" else "bot"
        warnings: list[str] = []
        try:
            page = self.feishu.get_messages(as_identity=identity, message_ids=[sent_message_id])
        except Exception as exc:
            return {
                "result": {"ok": False, "error": str(exc), "message_id": sent_message_id},
                "warnings": ["readback_failed"],
            }
        item = _find_message(page.items, sent_message_id)
        if item is None:
            return {
                "result": {"ok": False, "message_id": sent_message_id, "raw": page.raw},
                "warnings": ["readback_message_missing"],
            }
        raw = dict(item)
        raw["sent_by_agent"] = True
        message = self.normalizer.normalize(raw)
        inserted = self.store.upsert_message(message)
        if action.kind == "send_reply" and action.task_id is not None:
            self.store.record_agent_message_for_task(
                action.task_id,
                message,
                watch_until=_watch_until(),
            )
        if action.kind == "send_reply":
            target = _target_message_id(action)
            if message.reply_to_message_id and target and message.reply_to_message_id != target:
                warnings.append("readback_reply_target_mismatch")
            elif not message.reply_to_message_id:
                warnings.append("readback_reply_target_unavailable")
            expected_mentions = _expected_mentions(_payload_text(action))
            if expected_mentions and not message.mentions:
                warnings.append("readback_mentions_unavailable")
            elif expected_mentions and not expected_mentions <= set(message.mentions):
                warnings.append("readback_mentions_mismatch")
        return {
            "result": {
                "ok": True,
                "message_id": sent_message_id,
                "inserted": inserted,
                "reply_to_message_id": message.reply_to_message_id,
                "mentions": message.mentions,
                "raw": page.raw,
            },
            "warnings": warnings,
        }


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


def _exception_command_result(action: ActionRecord, stage: str, exc: Exception) -> dict[str, Any]:
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


def _exception_readback_result(sent_message_id: str | None, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "message_id": sent_message_id,
        "error": str(exc),
        "exception_type": type(exc).__name__,
    }


def _local_error(action: ActionRecord, message: str) -> LarkCliResult:
    return LarkCliResult(argv=["dispatch", action.kind, str(action.id)], exit_code=None, error=message)


def _target_message_id(action: ActionRecord) -> str | None:
    value = action.target_message_id or action.payload.get("reply_target_message_id") or action.payload.get("target_message_id")
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
            lines.append(f"{key}: {value}")
    preview = payload.get("preview")
    if isinstance(preview, str) and preview:
        lines.append(f"preview: {preview}")
    commands = payload.get("commands")
    if isinstance(commands, list) and commands:
        lines.append("commands:")
        lines.extend(str(command) for command in commands)
    return "\n".join(lines)


def _extract_message_id(value: Any, *, exclude: str | None = None) -> str | None:
    for key in ("sent_message_id", "sentMessageId", "message_id", "messageId"):
        found = _find_key(value, key)
        if isinstance(found, str) and found and found != exclude:
            return found
    return None


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, key)
            if found is not None:
                return found
    return None


def _find_message(items: list[dict[str, Any]], message_id: str) -> dict[str, Any] | None:
    for item in items:
        found = item.get("message_id") or item.get("messageId") or item.get("id")
        if found == message_id:
            return item
    if len(items) == 1:
        return items[0]
    return None


def _expected_mentions(text: str) -> set[str]:
    return set(EXPECTED_MENTION_RE.findall(text))


def _watch_until() -> str:
    return (datetime.now().astimezone() + timedelta(minutes=WATCH_EXTEND_MINUTES)).isoformat(timespec="seconds")


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
