from __future__ import annotations

import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import ValidationError

from .agent_backend import OwnerStyleBackend
from .agent_invocation import AgentInvoker, agent_result_error, truncate_error
from .config import AppConfig
from .message_normalization import MessageNormalizer
from .p2p_scope import P2PScopeClient, list_non_bot_p2p_chats
from .paths import resolve_agent_working_dir, resolve_relative_path
from .prompt import OwnerStyleRefreshOutput, build_owner_style_refresh_prompt
from .store.sqlite_store import SQLiteStore
from .time_utils import shift_instant
from .types import MessagePage, utc_now_iso


class ReplyStyleFeishuClient(Protocol):
    def search_owner_messages(
        self,
        *,
        sender: str,
        start: str | None,
        end: str | None,
        chat_type: str | None = None,
        chat_id: str | None = None,
        page_token: str | None = None,
        page_all: bool = True,
    ) -> MessagePage: ...


MAX_SAMPLE_CHARS = 1000
LINK_ONLY_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)
RESOURCE_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:\[(?:图片|image|文件|file|附件|attachment)\]|<[^>]+>)\s*$", re.IGNORECASE
)
OPERATOR_COMMAND_RE = re.compile(
    r"^/(?:approve|reject|send|task|dispatch|maintenance|policy)\b", re.IGNORECASE
)
PROFILE_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
PROFILE_FEISHU_ID_RE = re.compile(r"\b(?:ou|oc|om|on)_[A-Za-z0-9_-]{4,}\b")
PROFILE_PHONE_RE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|\+\d[\d -]{7,}\d)(?!\d)")


@dataclass(frozen=True)
class ReplyStyleRefreshResult:
    status: str
    pulled_count: int
    filtered_count: int
    selected_count: int
    profile_path: str
    wrote_profile: bool
    hermes_called: bool
    stats: dict[str, Any]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status,
            "pulled_count": self.pulled_count,
            "filtered_count": self.filtered_count,
            "selected_count": self.selected_count,
            "profile_path": self.profile_path,
            "wrote_profile": self.wrote_profile,
            "hermes_called": self.hermes_called,
            "stats": self.stats,
        }
        if self.error:
            data["error"] = self.error
        return data


class ReplyStyleRefresher:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: SQLiteStore,
        base_dir: str | Path,
        feishu_client: ReplyStyleFeishuClient,
        agent_backend: OwnerStyleBackend,
        agent_invoker: AgentInvoker,
    ):
        self.config = config
        self.store = store
        self.base_dir = Path(base_dir)
        self.feishu_client = feishu_client
        self.agent_backend = agent_backend
        self.agent_invoker = agent_invoker
        self.normalizer = MessageNormalizer(owner_open_id=config.owner.open_id)

    def refresh(self, *, dry_run: bool, run_id: str) -> ReplyStyleRefreshResult:
        refresh_cfg = self.config.reply_postprocess.owner_style.refresh
        generated_at = utc_now_iso()
        start = _minus_days(generated_at, refresh_cfg.lookback_days)
        raws: list[dict[str, Any]] = []
        sampled_activations: set[tuple[str, int]] = set()
        for activation in self.store.list_chat_policy_activations():
            chat_id = str(activation["chat_id"])
            activation_id = activation["activation_id"]
            if activation_id is None or not self._activation_is_current(
                chat_id, int(activation_id)
            ):
                continue
            group_raws = self._group_samples(
                chat_id=chat_id,
                activation_id=int(activation_id),
                start=start,
                end=generated_at,
            )
            if group_raws is None:
                continue
            sampled_activations.add((chat_id, int(activation_id)))
            raws.extend(group_raws)
        p2p_scope_error: str | None = None
        if dry_run:
            allowed_chats: set[str]
            try:
                allowed_chats = list_non_bot_p2p_chats(
                    cast(P2PScopeClient, self.feishu_client)
                )
            except Exception as exc:  # noqa: BLE001
                allowed_chats = set()
                p2p_scope_error = str(exc)
            for chat_id in sorted(allowed_chats):
                page = self.feishu_client.search_owner_messages(
                    sender=self.config.owner.open_id,
                    start=start,
                    end=generated_at,
                    chat_type="p2p",
                    chat_id=chat_id,
                )
                raws.extend(
                    row
                    for row in page.items
                    if row.get("chat_id", row.get("chatId")) == chat_id
                )
        else:
            page = self.feishu_client.search_owner_messages(
                sender=self.config.owner.open_id,
                start=start,
                end=generated_at,
                chat_type="p2p",
            )
            raws.extend(
                row
                for row in page.items
                if row.get("chat_type", row.get("chatType")) != "group"
            )
        samples = self._filtered_samples(raws)
        selected = samples[: refresh_cfg.max_samples]
        profile_path = resolve_relative_path(
            self.config.reply_postprocess.owner_style.profile_path, self.base_dir
        )
        stats = _sample_stats(selected)
        if p2p_scope_error:
            stats["p2p_scope_error"] = p2p_scope_error
        if dry_run:
            return ReplyStyleRefreshResult(
                status="dry_run",
                pulled_count=len(raws),
                filtered_count=len(samples),
                selected_count=len(selected),
                profile_path=str(profile_path),
                wrote_profile=False,
                hermes_called=False,
                stats=stats,
            )
        if len(selected) < refresh_cfg.min_samples:
            return ReplyStyleRefreshResult(
                status="failed",
                pulled_count=len(raws),
                filtered_count=len(samples),
                selected_count=len(selected),
                profile_path=str(profile_path),
                wrote_profile=False,
                hermes_called=False,
                stats=stats,
                error=f"not enough owner reply samples: {len(selected)} < {refresh_cfg.min_samples}",
            )
        if any(
            not self._activation_is_current(chat_id, activation_id)
            for chat_id, activation_id in sampled_activations
        ):
            return ReplyStyleRefreshResult(
                status="failed",
                pulled_count=len(raws),
                filtered_count=len(samples),
                selected_count=len(selected),
                profile_path=str(profile_path),
                wrote_profile=False,
                hermes_called=False,
                stats=stats,
                error="group policy changed while selecting reply style samples",
            )
        prompt = build_owner_style_refresh_prompt(
            generated_at=generated_at,
            lookback_days=refresh_cfg.lookback_days,
            samples=selected,
        )
        cwd = resolve_agent_working_dir(
            self.config.agent_backend.working_dir, self.base_dir
        )
        outcome = self.agent_invoker.call_with_retries(
            lambda: self.agent_backend.owner_style_refresh(prompt, cwd=cwd),
            run_id=run_id,
            stage="owner_style_refresh",
            message_id="owner_style_refresh",
            task_id=None,
            before_attempt=lambda: all(
                self._activation_is_current(chat_id, activation_id)
                for chat_id, activation_id in sampled_activations
            ),
        )
        result = outcome.result
        if outcome.blocked or any(
            not self._activation_is_current(chat_id, activation_id)
            for chat_id, activation_id in sampled_activations
        ):
            return ReplyStyleRefreshResult(
                status="failed",
                pulled_count=len(raws),
                filtered_count=len(samples),
                selected_count=len(selected),
                profile_path=str(profile_path),
                wrote_profile=False,
                hermes_called=outcome.attempt_count > 0,
                stats=stats,
                error="group policy changed during reply style refresh",
            )
        if result is None or not result.ok or not isinstance(result.json_data, dict):
            error = outcome.last_error or (
                None if result is None else agent_result_error(result)
            )
            return ReplyStyleRefreshResult(
                status="failed",
                pulled_count=len(raws),
                filtered_count=len(samples),
                selected_count=len(selected),
                profile_path=str(profile_path),
                wrote_profile=False,
                hermes_called=True,
                stats=stats,
                error=truncate_error(error),
            )
        response_data: object = getattr(result, "json_data", None)
        try:
            output = OwnerStyleRefreshOutput.model_validate(
                cast(dict[str, Any], response_data)
            )
        except ValidationError as exc:
            return ReplyStyleRefreshResult(
                status="failed",
                pulled_count=len(raws),
                filtered_count=len(samples),
                selected_count=len(selected),
                profile_path=str(profile_path),
                wrote_profile=False,
                hermes_called=True,
                stats=stats,
                error=truncate_error(str(exc)),
            )
        profile_markdown = output.profile_markdown.strip()
        if output.status != "ok" or not profile_markdown:
            return ReplyStyleRefreshResult(
                status="failed",
                pulled_count=len(raws),
                filtered_count=len(samples),
                selected_count=len(selected),
                profile_path=str(profile_path),
                wrote_profile=False,
                hermes_called=True,
                stats=stats,
                error="owner style refresh did not return a profile",
            )
        profile_error = _profile_privacy_error(profile_markdown)
        if profile_error is not None:
            return ReplyStyleRefreshResult(
                status="failed",
                pulled_count=len(raws),
                filtered_count=len(samples),
                selected_count=len(selected),
                profile_path=str(profile_path),
                wrote_profile=False,
                hermes_called=True,
                stats=stats,
                error=profile_error,
            )
        _atomic_write_text(profile_path, profile_markdown + "\n")
        return ReplyStyleRefreshResult(
            status="written",
            pulled_count=len(raws),
            filtered_count=len(samples),
            selected_count=len(selected),
            profile_path=str(profile_path),
            wrote_profile=True,
            hermes_called=True,
            stats=stats,
        )

    def _group_samples(
        self, *, chat_id: str, activation_id: int, start: str, end: str
    ) -> list[dict[str, Any]] | None:
        rows: list[dict[str, Any]] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(1000):
            if not self._activation_is_current(chat_id, activation_id):
                return None
            page = self.feishu_client.search_owner_messages(
                sender=self.config.owner.open_id,
                start=start,
                end=end,
                chat_type="group",
                chat_id=chat_id,
                page_token=token,
                page_all=False,
            )
            if not self._activation_is_current(chat_id, activation_id):
                return None
            rows.extend(
                row
                for row in page.items
                if row.get("chat_id", row.get("chatId")) == chat_id
            )
            if not page.has_more:
                return rows
            token = page.next_page_token
            if not token or token in seen_tokens:
                raise ValueError("group reply-style pagination is incomplete")
            seen_tokens.add(token)
        raise ValueError("group reply-style pagination exceeded its safety limit")

    def _activation_is_current(self, chat_id: str, activation_id: int) -> bool:
        return any(
            row["chat_id"] == chat_id and row["activation_id"] == activation_id
            for row in self.store.list_chat_policy_activations()
        )

    def _filtered_samples(self, raws: list[dict[str, Any]]) -> list[str]:
        samples: list[str] = []
        for raw in raws:
            try:
                message = self.normalizer.normalize(raw)
            except ValueError:
                continue
            text = " ".join(message.text.split())
            if not _keep_sample(text):
                continue
            samples.append(text)
        return samples


def _keep_sample(text: str) -> bool:
    if not text:
        return False
    if OPERATOR_COMMAND_RE.match(text):
        return False
    if LINK_ONLY_RE.match(text):
        return False
    if RESOURCE_PLACEHOLDER_RE.match(text):
        return False
    return len(text) <= MAX_SAMPLE_CHARS


def _sample_stats(samples: list[str]) -> dict[str, Any]:
    lengths = [len(sample) for sample in samples]
    if not lengths:
        return {"min_chars": 0, "max_chars": 0, "avg_chars": 0}
    return {
        "min_chars": min(lengths),
        "max_chars": max(lengths),
        "avg_chars": round(sum(lengths) / len(lengths), 1),
    }


def _profile_privacy_error(profile_markdown: str) -> str | None:
    if PROFILE_URL_RE.search(profile_markdown):
        return "owner style profile contains a URL"
    if PROFILE_FEISHU_ID_RE.search(profile_markdown):
        return "owner style profile contains a Feishu identifier"
    if PROFILE_PHONE_RE.search(profile_markdown):
        return "owner style profile contains a phone number"
    return None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except Exception:
        # Cleanup must cover encoding and other non-OS write failures too.
        with suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def _minus_days(value: str, days: int) -> str:
    return shift_instant(value, delta=-timedelta(days=days))
