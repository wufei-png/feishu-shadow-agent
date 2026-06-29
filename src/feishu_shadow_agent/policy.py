from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import AppConfig, ChatPolicyConfig
from .types import NormalizedMessage, TaskRecord

PolicySource = Literal["explicit_chat", "unknown_group", "p2p", "default"]


@dataclass(frozen=True)
class ResolvedChatPolicy:
    auto_reply: bool
    resource_download: bool
    bot_joined: bool
    reply_identity: Literal["bot_preferred", "bot", "user"]
    allow_user_fallback: bool
    policy_source: PolicySource


@dataclass(frozen=True)
class ResourcePolicyDecision:
    allow: bool
    reason: str
    policy_source: PolicySource


@dataclass(frozen=True)
class ReplyPolicyDecision:
    allow: bool
    reason: str
    identity: Literal["bot", "user"]
    policy_source: PolicySource


class PolicyResolver:
    def __init__(self, config: AppConfig):
        self.config = config

    def resolve_chat_policy(self, chat_id: str | None, chat_type: str | None) -> ResolvedChatPolicy:
        if chat_id and chat_id in self.config.chats:
            return _resolved_from_config(self.config.chats[chat_id], policy_source="explicit_chat")
        default = ChatPolicyConfig()
        if chat_type == "p2p":
            return _resolved_from_config(default, auto_reply=self.config.reply_policy.p2p_auto_reply, policy_source="p2p")
        if chat_type == "group":
            return _resolved_from_config(
                default,
                auto_reply=self.config.reply_policy.unknown_group_auto_reply,
                policy_source="unknown_group",
            )
        return _resolved_from_config(default, policy_source="default")

    def can_download_resources(self, message: NormalizedMessage) -> ResourcePolicyDecision:
        policy = self.resolve_chat_policy(message.chat_id, message.chat_type)
        if not policy.resource_download:
            return ResourcePolicyDecision(False, "disabled_by_chat_policy", policy.policy_source)
        if not policy.bot_joined:
            return ResourcePolicyDecision(False, "bot_not_joined", policy.policy_source)
        return ResourcePolicyDecision(True, "ok", policy.policy_source)

    def resolve_reply_policy(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        answerability: str,
        had_forbidden_mentions: bool,
        proposed_reply: str,
        final_reply: str,
    ) -> ReplyPolicyDecision:
        chat_type = task.chat_type or message.chat_type
        policy = self.resolve_chat_policy(task.chat_id or message.chat_id, chat_type)
        if answerability != "auto_reply":
            return ReplyPolicyDecision(False, "needs_owner", "user", policy.policy_source)
        if had_forbidden_mentions:
            return ReplyPolicyDecision(False, "forbidden_mentions", "user", policy.policy_source)
        if not proposed_reply.strip() or not final_reply.strip():
            return ReplyPolicyDecision(False, "empty_proposed_reply", "user", policy.policy_source)
        if chat_type == "p2p":
            if not self.config.reply_policy.p2p_auto_reply:
                return ReplyPolicyDecision(False, "p2p_auto_reply_disabled", "user", policy.policy_source)
            return ReplyPolicyDecision(True, "ok", "user", policy.policy_source)
        if chat_type == "group":
            if not message.direct_mention:
                return ReplyPolicyDecision(False, "group_not_direct_mention", "user", policy.policy_source)
            if not policy.auto_reply:
                reason = (
                    "unknown_group_auto_reply_disabled"
                    if policy.policy_source == "unknown_group"
                    else "group_auto_reply_disabled"
                )
                return ReplyPolicyDecision(False, reason, "user", policy.policy_source)
            if policy.reply_identity in {"bot", "bot_preferred"} and policy.bot_joined:
                return ReplyPolicyDecision(True, "ok", "bot", policy.policy_source)
            if policy.reply_identity == "user":
                return ReplyPolicyDecision(True, "ok", "user", policy.policy_source)
            if policy.reply_identity == "bot_preferred" and policy.allow_user_fallback:
                return ReplyPolicyDecision(True, "ok", "user", policy.policy_source)
            return ReplyPolicyDecision(False, "bot_not_joined", "bot", policy.policy_source)
        return ReplyPolicyDecision(False, "unknown_chat_type", "user", policy.policy_source)


def _resolved_from_config(
    config: ChatPolicyConfig,
    *,
    auto_reply: bool | None = None,
    policy_source: PolicySource,
) -> ResolvedChatPolicy:
    return ResolvedChatPolicy(
        auto_reply=config.auto_reply if auto_reply is None else auto_reply,
        resource_download=config.resource_download,
        bot_joined=config.bot_joined,
        reply_identity=config.reply_identity,
        allow_user_fallback=config.allow_user_fallback,
        policy_source=policy_source,
    )
