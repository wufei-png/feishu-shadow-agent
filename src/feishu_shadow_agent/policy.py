from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from .config import ChatPolicyConfig, ReplyPolicyConfig
from .types import NormalizedMessage, TaskRecord

PolicySource = Literal["explicit_chat", "unknown_group", "p2p", "default"]


class ProductPolicyRepository(Protocol):
    def get_product_policy(self) -> dict[str, Any] | None: ...

    def get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None: ...


class ProductPolicyMissingError(RuntimeError):
    pass


class ProductPolicyInvalidError(RuntimeError):
    pass


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
    def __init__(self, repository: ProductPolicyRepository):
        self.repository = repository

    def resolve_chat_policy(
        self, chat_id: str | None, chat_type: str | None
    ) -> ResolvedChatPolicy:
        global_policy = self._global_product_policy()
        if chat_id:
            chat_policy = self.repository.get_chat_product_policy(chat_id)
            if chat_policy is not None:
                return _resolved_from_policy(chat_policy, policy_source="explicit_chat")
        reply_policy = _reply_policy_config(global_policy)
        default = _default_chat_policy_config(global_policy)
        if chat_type == "p2p":
            return _resolved_from_config(
                default, auto_reply=reply_policy.p2p_auto_reply, policy_source="p2p"
            )
        if chat_type == "group":
            return _resolved_from_config(
                default,
                auto_reply=reply_policy.unknown_group_auto_reply,
                policy_source="unknown_group",
            )
        return _resolved_from_config(default, policy_source="default")

    def can_download_resources(
        self, message: NormalizedMessage
    ) -> ResourcePolicyDecision:
        policy = self.resolve_chat_policy(message.chat_id, message.chat_type)
        if not policy.resource_download:
            return ResourcePolicyDecision(
                False, "disabled_by_chat_policy", policy.policy_source
            )
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
            return ReplyPolicyDecision(
                False, "needs_owner", "user", policy.policy_source
            )
        if had_forbidden_mentions:
            return ReplyPolicyDecision(
                False, "forbidden_mentions", "user", policy.policy_source
            )
        if not proposed_reply.strip() or not final_reply.strip():
            return ReplyPolicyDecision(
                False, "empty_proposed_reply", "user", policy.policy_source
            )
        if chat_type == "p2p":
            if not _reply_policy_config(self._global_product_policy()).p2p_auto_reply:
                return ReplyPolicyDecision(
                    False, "p2p_auto_reply_disabled", "user", policy.policy_source
                )
            return ReplyPolicyDecision(True, "ok", "user", policy.policy_source)
        if chat_type == "group":
            if not message.direct_mention:
                return ReplyPolicyDecision(
                    False, "group_not_direct_mention", "user", policy.policy_source
                )
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
            return ReplyPolicyDecision(
                False, "bot_not_joined", "bot", policy.policy_source
            )
        return ReplyPolicyDecision(
            False, "unknown_chat_type", "user", policy.policy_source
        )

    def _global_product_policy(self) -> dict[str, Any]:
        policy = self.repository.get_product_policy()
        if policy is None:
            raise ProductPolicyMissingError(
                "Product Policy Store global policy is not initialized; run `policy import-config`."
            )
        if "reply_policy" not in policy or "default_chat_policy" not in policy:
            raise ProductPolicyInvalidError(
                "Product Policy Store global policy is missing required fields."
            )
        return policy


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


def _resolved_from_policy(
    policy: dict[str, Any], *, policy_source: PolicySource
) -> ResolvedChatPolicy:
    return _resolved_from_config(
        _chat_policy_config(policy), policy_source=policy_source
    )


def _reply_policy_config(global_policy: dict[str, Any]) -> ReplyPolicyConfig:
    try:
        return ReplyPolicyConfig.model_validate(global_policy["reply_policy"])
    except (KeyError, TypeError, ValidationError) as exc:
        raise ProductPolicyInvalidError(
            "Product Policy Store reply_policy is invalid."
        ) from exc


def _default_chat_policy_config(global_policy: dict[str, Any]) -> ChatPolicyConfig:
    try:
        return ChatPolicyConfig.model_validate(global_policy["default_chat_policy"])
    except (KeyError, TypeError, ValidationError) as exc:
        raise ProductPolicyInvalidError(
            "Product Policy Store default_chat_policy is invalid."
        ) from exc


def _chat_policy_config(policy: dict[str, Any]) -> ChatPolicyConfig:
    data = {key: value for key, value in policy.items() if key != "chat_id"}
    try:
        return ChatPolicyConfig.model_validate(data)
    except (TypeError, ValidationError) as exc:
        raise ProductPolicyInvalidError(
            "Product Policy Store chat policy is invalid."
        ) from exc
