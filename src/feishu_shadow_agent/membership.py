from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol, cast

from .config import AppConfig
from .jsonl import JSONLLogger
from .store.sqlite_store import SQLiteStore
from .time_utils import normalize_instant, parse_instant_or_none, shift_instant
from .types import ExecutionMode, LarkCliResult, utc_now_iso

MembershipStatus = Literal["present", "absent", "unknown"]


class MembershipFeishuClient(Protocol):
    def auth_status(self, *, verify: bool = True) -> LarkCliResult: ...

    def list_chat_bots(
        self, *, chat_id: str, as_identity: str = "user"
    ) -> LarkCliResult: ...


@dataclass(frozen=True)
class BotMembershipRefreshSummary:
    candidates: int = 0
    probed: int = 0
    present: int = 0
    absent: int = 0
    unknown: int = 0
    notifications: int = 0


@dataclass(frozen=True)
class BotMembershipAbsence:
    endpoint: str
    error_code: int


_BOT_MEMBERSHIP_ABSENCE_ENDPOINTS = {
    ("im", "+messages-resources-download"): "resource_download",
    ("im", "+messages-reply"): "message_reply",
}
_BOT_NOT_IN_CHAT_ERROR_CODE = 10002


class BotMembershipService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        feishu_client: MembershipFeishuClient,
        config: AppConfig,
        logger: JSONLLogger,
        clock: Callable[[], str] = utc_now_iso,
        execution_mode: ExecutionMode = "production",
    ):
        self.store = store
        self.feishu = feishu_client
        self.config = config
        self.logger = logger
        self.clock = lambda: normalize_instant(clock())
        self.execution_mode: ExecutionMode = execution_mode

    def refresh(self, *, run_id: str) -> BotMembershipRefreshSummary:
        now = self.clock()
        candidates = self.store.list_bot_membership_candidate_chats()
        due = [chat_id for chat_id in candidates if self._probe_due(chat_id, now=now)]
        if not due:
            return BotMembershipRefreshSummary(candidates=len(candidates))
        auth = self.feishu.auth_status(verify=True)
        bot_open_id = _bot_open_id_from_auth(auth.json_data) if auth.ok else None
        summary = BotMembershipRefreshSummary(candidates=len(candidates))
        for chat_id in due:
            if bot_open_id is None:
                status: MembershipStatus = "unknown"
                error = auth.error or "bot open_id is missing from auth status"
            else:
                try:
                    result = self.feishu.list_chat_bots(
                        chat_id=chat_id, as_identity="user"
                    )
                except Exception as exc:  # noqa: BLE001
                    status = "unknown"
                    error = str(exc)
                else:
                    if not result.ok:
                        status = "unknown"
                        error = (
                            result.error or result.stderr or "membership probe failed"
                        )
                    else:
                        status = (
                            "present"
                            if bot_open_id in _bot_ids(result.json_data)
                            else "absent"
                        )
                        error = None
            notified = _record_observation(
                store=self.store,
                config=self.config,
                logger=self.logger,
                execution_mode=self.execution_mode,
                chat_id=chat_id,
                status=status,
                checked_at=now,
                source="active_probe",
                error=error,
                run_id=run_id,
            )
            summary = BotMembershipRefreshSummary(
                candidates=summary.candidates,
                probed=summary.probed + 1,
                present=summary.present + (status == "present"),
                absent=summary.absent + (status == "absent"),
                unknown=summary.unknown + (status == "unknown"),
                notifications=summary.notifications + int(notified),
            )
        return summary

    def _probe_due(self, chat_id: str, *, now: str) -> bool:
        fact = self.store.get_bot_membership_fact(chat_id)
        if fact is None:
            return True
        next_probe = parse_instant_or_none(fact.get("next_probe_at"))
        observed = parse_instant_or_none(now)
        return next_probe is None or observed is None or observed >= next_probe


def record_bot_membership_absence(
    *,
    store: SQLiteStore,
    config: AppConfig,
    logger: JSONLLogger,
    chat_id: str | None,
    chat_type: str | None,
    run_id: str | None,
    source: str,
    error: str | None,
    absence: BotMembershipAbsence,
    execution_mode: ExecutionMode = "production",
) -> None:
    if not chat_id or chat_type != "group":
        return
    _record_observation(
        store=store,
        config=config,
        logger=logger,
        execution_mode=execution_mode,
        chat_id=chat_id,
        status="absent",
        checked_at=normalize_instant(utc_now_iso()),
        source=source,
        error=error,
        error_code=absence.error_code,
        error_endpoint=absence.endpoint,
        run_id=run_id,
    )


def _record_observation(
    *,
    store: SQLiteStore,
    config: AppConfig,
    logger: JSONLLogger,
    execution_mode: ExecutionMode,
    chat_id: str,
    status: MembershipStatus,
    checked_at: str,
    source: str,
    error: str | None,
    run_id: str | None,
    error_code: int | None = None,
    error_endpoint: str | None = None,
) -> bool:
    previous = store.get_bot_membership_fact(chat_id) or {}
    previous_status = previous.get("status")
    previous_confirmed_status = previous.get("last_confirmed_status")
    if previous_confirmed_status not in {"present", "absent"}:
        previous_confirmed_status = (
            previous_status if previous_status in {"present", "absent"} else None
        )
    episode = int(previous.get("absence_episode", 0))
    if status == "absent" and previous_confirmed_status != "absent":
        episode += 1
    interval = (
        config.daemon.bot_membership_retry_seconds
        if status == "unknown"
        else config.daemon.bot_membership_ttl_seconds
    )
    fact = {
        "status": status,
        "checked_at": checked_at,
        "next_probe_at": shift_instant(checked_at, delta=timedelta(seconds=interval)),
        "source": source,
        "error": error,
        "error_code": error_code,
        "error_endpoint": error_endpoint,
        "absence_episode": episode,
        "last_confirmed_status": (
            status if status in {"present", "absent"} else previous_confirmed_status
        ),
    }
    store.set_bot_membership_fact(chat_id, fact)
    notification_id: int | None = None
    should_notify = (status == "absent" and previous_confirmed_status != "absent") or (
        status == "present" and previous_confirmed_status == "absent"
    )
    if should_notify:
        notification_id = store.create_owner_notification_action(
            task_id=None,
            payload={
                "type": "bot_membership_absent"
                if status == "absent"
                else "bot_membership_recovered",
                "chat_id": chat_id,
                "membership_status": status,
                "dedupe_key": f"bot-membership:{chat_id}:{status}:{episode}",
            },
            execution_mode=execution_mode,
        )
    logger.emit(
        "warning" if status != "present" else "info",
        "bot_membership_observed",
        run_id=run_id,
        data={
            "chat_id": chat_id,
            "status": status,
            "previous_status": previous_status,
            "previous_confirmed_status": previous_confirmed_status,
            "source": source,
            "error": error,
            "error_code": error_code,
            "error_endpoint": error_endpoint,
            "next_probe_at": fact["next_probe_at"],
            "notification_action_id": notification_id,
        },
    )
    return notification_id is not None


def classify_bot_membership_absence(
    result: LarkCliResult,
) -> BotMembershipAbsence | None:
    """Return confirmed bot absence only for a supported structured API error."""
    if result.ok or not _command_uses_bot(result):
        return None
    if len(result.argv) < 3:
        return None
    endpoint = _BOT_MEMBERSHIP_ABSENCE_ENDPOINTS.get((result.argv[1], result.argv[2]))
    error_code = _structured_error_code(result.json_data)
    if endpoint is None or error_code != _BOT_NOT_IN_CHAT_ERROR_CODE:
        return None
    return BotMembershipAbsence(endpoint=endpoint, error_code=error_code)


def effective_membership_status(
    fact: dict[str, Any] | None, *, now: str | None = None
) -> MembershipStatus | Literal["unobserved"]:
    if fact is None:
        return "unobserved"
    status = fact.get("status")
    next_probe = parse_instant_or_none(fact.get("next_probe_at"))
    observed = parse_instant_or_none(now or utc_now_iso())
    if status not in {"present", "absent", "unknown"}:
        return "unknown"
    if next_probe is None or observed is None or observed >= next_probe:
        return "unknown"
    return cast(MembershipStatus, status)


def _bot_ids(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    mapping = cast(dict[str, Any], value)
    nested = mapping.get("data")
    source = cast(dict[str, Any], nested) if isinstance(nested, dict) else mapping
    items_value = source.get("items")
    if not isinstance(items_value, list):
        return set()
    items = cast(list[Any], items_value)
    return {
        str(item["bot_id"])
        for raw_item in items
        if isinstance(raw_item, dict)
        for item in [cast(dict[str, Any], raw_item)]
        if item.get("bot_id")
    }


def _command_uses_bot(result: LarkCliResult) -> bool:
    return any(
        result.argv[index : index + 2] == ["--as", "bot"]
        for index in range(max(0, len(result.argv) - 1))
    )


def _structured_error_code(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    source = cast(dict[str, Any], value)
    code = source.get("code")
    if code is None:
        error = source.get("error")
        if isinstance(error, dict):
            code = cast(dict[str, Any], error).get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    if isinstance(code, str) and code.isdecimal():
        return int(code)
    return None


def _bot_open_id_from_auth(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    identities = cast(dict[str, Any], value).get("identities")
    if not isinstance(identities, dict):
        return None
    bot = cast(dict[str, Any], identities).get("bot")
    if not isinstance(bot, dict):
        return None
    bot_map = cast(dict[str, Any], bot)
    for key in ("openId", "open_id", "openID", "id"):
        result = bot_map.get(key)
        if isinstance(result, str) and result:
            return result
    return None
