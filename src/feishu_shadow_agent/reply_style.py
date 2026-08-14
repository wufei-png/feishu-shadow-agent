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
from .ingestion import MessageNormalizer
from .paths import resolve_agent_working_dir, resolve_relative_path
from .prompt import OwnerStyleRefreshOutput, build_owner_style_refresh_prompt
from .time_utils import shift_instant
from .types import MessagePage, utc_now_iso


class ReplyStyleFeishuClient(Protocol):
    def search_owner_messages(
        self,
        *,
        sender: str,
        start: str | None,
        end: str | None,
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
        base_dir: str | Path,
        feishu_client: ReplyStyleFeishuClient,
        agent_backend: OwnerStyleBackend,
        agent_invoker: AgentInvoker,
    ):
        self.config = config
        self.base_dir = Path(base_dir)
        self.feishu_client = feishu_client
        self.agent_backend = agent_backend
        self.agent_invoker = agent_invoker
        self.normalizer = MessageNormalizer(owner_open_id=config.owner.open_id)

    def refresh(self, *, dry_run: bool, run_id: str) -> ReplyStyleRefreshResult:
        refresh_cfg = self.config.reply_postprocess.owner_style.refresh
        generated_at = utc_now_iso()
        start = _minus_days(generated_at, refresh_cfg.lookback_days)
        page = self.feishu_client.search_owner_messages(
            sender=self.config.owner.open_id,
            start=start,
            end=generated_at,
        )
        samples = self._filtered_samples(page.items)
        selected = samples[: refresh_cfg.max_samples]
        profile_path = resolve_relative_path(
            self.config.reply_postprocess.owner_style.profile_path, self.base_dir
        )
        stats = _sample_stats(selected)
        if dry_run:
            return ReplyStyleRefreshResult(
                status="dry_run",
                pulled_count=len(page.items),
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
                pulled_count=len(page.items),
                filtered_count=len(samples),
                selected_count=len(selected),
                profile_path=str(profile_path),
                wrote_profile=False,
                hermes_called=False,
                stats=stats,
                error=f"not enough owner reply samples: {len(selected)} < {refresh_cfg.min_samples}",
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
        )
        result = outcome.result
        if result is None or not result.ok or not isinstance(result.json_data, dict):
            error = outcome.last_error or (
                None if result is None else agent_result_error(result)
            )
            return ReplyStyleRefreshResult(
                status="failed",
                pulled_count=len(page.items),
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
                pulled_count=len(page.items),
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
                pulled_count=len(page.items),
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
                pulled_count=len(page.items),
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
            pulled_count=len(page.items),
            filtered_count=len(samples),
            selected_count=len(selected),
            profile_path=str(profile_path),
            wrote_profile=True,
            hermes_called=True,
            stats=stats,
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
