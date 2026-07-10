from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .types import NormalizedMessage

ACQUISITION_SOURCES = {"group_at_me", "active_watch", "p2p"}


@dataclass(frozen=True)
class MessageEligibility:
    eligible: bool
    reason_code: str

    @property
    def decision(self) -> str:
        return "kept" if self.eligible else "dropped"


class MessageEligibilityPolicy:
    def decide(
        self, message: NormalizedMessage, *, sources: Iterable[str]
    ) -> MessageEligibility:
        source_set = set(sources)
        unknown = source_set - ACQUISITION_SOURCES
        if unknown:
            raise ValueError(f"unknown acquisition sources: {sorted(unknown)}")
        if not source_set:
            return MessageEligibility(False, "not_acquired")
        if message.is_self_message:
            return MessageEligibility(False, "self_message")
        if message.at_all:
            return MessageEligibility(False, "at_all_suppressed")
        if message.chat_type == "p2p":
            if source_set == {"p2p"}:
                return MessageEligibility(True, "p2p_message")
            if source_set == {"active_watch"}:
                return MessageEligibility(True, "active_watch_message")
            raise ValueError(
                "p2p messages require exactly one of p2p or active_watch source"
            )
        if message.chat_type != "group":
            raise ValueError(f"unsupported message chat_type: {message.chat_type}")
        if "p2p" in source_set:
            raise ValueError("group messages cannot use the p2p source")
        if message.sender_role == "owner_message":
            return MessageEligibility(True, "owner_intervention")
        if "group_at_me" in source_set and message.direct_mention:
            return MessageEligibility(True, "direct_owner_mention")
        if "active_watch" in source_set:
            return MessageEligibility(True, "active_watch_message")
        if source_set == {"group_at_me"}:
            return MessageEligibility(False, "non_direct_mention")
        raise ValueError(
            f"unsupported acquisition source combination: {sorted(source_set)}"
        )
