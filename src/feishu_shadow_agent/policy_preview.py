from __future__ import annotations

from dataclasses import asdict
from typing import Any

from pydantic import ValidationError

from .config import ChatPolicyConfig
from .operator_commands import (
    CHAT_POLICY_UPDATE_FIELDS,
    GLOBAL_POLICY_UPDATE_FIELDS,
    _merged_chat_policy,
    _merged_global_policy,
    _policy_changes,
)
from .policy import (
    PolicyResolver,
    ProductPolicyInvalidError,
    ProductPolicyMissingError,
)
from .store.sqlite_store import SQLiteStore

_GLOBAL_FIELDS = (
    "p2p_auto_reply",
    "unknown_group_auto_reply",
    "bot_joined",
    "reply_identity",
    "allow_user_fallback",
    "resource_download",
)
_CHAT_FIELDS = (
    "name",
    "auto_reply",
    "bot_joined",
    "reply_identity",
    "allow_user_fallback",
    "resource_download",
)
_EFFECTIVE_FIELDS = (
    "auto_reply",
    "bot_joined",
    "reply_identity",
    "allow_user_fallback",
    "resource_download",
)


class PolicyPreviewValidationError(ValueError):
    pass


class PolicyPreviewNotFoundError(KeyError):
    pass


class PolicyPreviewService:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def preview_global_policy(self, changes: dict[str, Any]) -> dict[str, Any]:
        normalized_changes = _normalized_changes(
            changes, allowed_fields=GLOBAL_POLICY_UPDATE_FIELDS
        )
        old_policy = self.store.get_product_policy()
        if old_policy is None:
            raise ProductPolicyMissingError(
                "global Product Policy is not initialized; run `policy import-config` first"
            )
        try:
            new_policy = _merged_global_policy(old_policy, normalized_changes)
        except (TypeError, ValidationError, ValueError) as exc:
            raise PolicyPreviewValidationError(str(exc)) from exc

        before = _global_effective_preview(old_policy)
        after = _global_effective_preview(new_policy)
        return {
            "scope": "global",
            "operation": "update",
            "target": {"type": "global_policy", "key": "reply_policy"},
            "field_changes": _global_field_changes(old_policy, new_policy),
            "effective_before": before,
            "effective_after": after,
            "behavior_changes": _effective_group_changes(before, after),
            "affected_summary": {
                "p2p_auto_reply_uses_global": True,
                "unknown_group_auto_reply_uses_global": True,
                "default_applies_to": "chats without explicit chat policy rows",
                "explicit_chat_policy_count": _chat_policy_count(self.store),
                "explicit_chat_policies_changed": False,
            },
            "warnings": [],
        }

    def preview_chat_policy(
        self, chat_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        normalized_chat_id = chat_id.strip()
        if not normalized_chat_id:
            raise PolicyPreviewValidationError("chat_id is required")
        normalized_changes = _normalized_changes(
            changes, allowed_fields=CHAT_POLICY_UPDATE_FIELDS
        )
        global_policy = self.store.get_product_policy()
        if global_policy is None:
            raise ProductPolicyMissingError(
                "global Product Policy is not initialized; run `policy import-config` first"
            )
        old_policy = self.store.get_chat_product_policy(normalized_chat_id)
        base_policy = old_policy or {
            "chat_id": normalized_chat_id,
            **ChatPolicyConfig().model_dump(mode="json"),
        }
        try:
            new_policy = _merged_chat_policy(base_policy, normalized_changes)
        except (TypeError, ValidationError, ValueError) as exc:
            raise PolicyPreviewValidationError(str(exc)) from exc

        before = (
            _effective_from_chat_policy(old_policy)
            if old_policy is not None
            else _effective_for_group_fallback(global_policy, normalized_chat_id)
        )
        after = _effective_from_chat_policy(new_policy)
        return {
            "scope": "chat",
            "operation": "update",
            "target": {"type": "chat_policy", "chat_id": normalized_chat_id},
            "field_changes": _chat_field_changes(base_policy, new_policy),
            "effective_before": before,
            "effective_after": after,
            "behavior_changes": _effective_changes(before, after),
            "affected_summary": {
                "chat_id": normalized_chat_id,
                "explicit_chat_policy_exists": old_policy is not None,
                "will_create_chat_policy": old_policy is None,
                "fallback_before_update": old_policy is None,
            },
            "warnings": [],
        }

    def preview_delete_chat_policy(self, chat_id: str) -> dict[str, Any]:
        normalized_chat_id = chat_id.strip()
        if not normalized_chat_id:
            raise PolicyPreviewValidationError("chat_id is required")
        old_policy = self.store.get_chat_product_policy(normalized_chat_id)
        if old_policy is None:
            raise PolicyPreviewNotFoundError(
                f"chat policy not found: {normalized_chat_id}"
            )

        before = _effective_from_chat_policy(old_policy)
        global_policy = self.store.get_product_policy()
        warnings: list[str] = []
        if global_policy is None:
            after = {
                "policy_source": "uninitialized",
                "auto_reply": None,
                "bot_joined": None,
                "reply_identity": None,
                "allow_user_fallback": None,
                "resource_download": None,
                "error": "global Product Policy is not initialized; fallback cannot be resolved",
            }
            warnings.append(
                "Global Product Policy is not initialized; delete can remove the override but fallback behavior cannot be resolved."
            )
        else:
            after = _effective_for_group_fallback(global_policy, normalized_chat_id)

        return {
            "scope": "chat",
            "operation": "delete",
            "target": {"type": "chat_policy", "chat_id": normalized_chat_id},
            "field_changes": _chat_field_changes(old_policy, None),
            "effective_before": before,
            "effective_after": after,
            "behavior_changes": _effective_changes(before, after),
            "affected_summary": {
                "chat_id": normalized_chat_id,
                "explicit_chat_policy_exists": True,
                "will_remove_chat_policy": True,
                "fallback_policy_source": after.get("policy_source"),
            },
            "warnings": warnings,
        }


class _PreviewRepository:
    def __init__(
        self,
        global_policy: dict[str, Any],
        chat_policies: dict[str, dict[str, Any] | None] | None = None,
    ):
        self.global_policy = global_policy
        self.chat_policies = chat_policies or {}

    def get_product_policy(self) -> dict[str, Any] | None:
        return self.global_policy

    def get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None:
        return self.chat_policies.get(chat_id)


def _normalized_changes(
    changes: dict[str, Any], *, allowed_fields: set[str]
) -> dict[str, Any]:
    try:
        normalized = _policy_changes(changes, allowed_fields=allowed_fields)
    except ValueError as exc:
        raise PolicyPreviewValidationError(str(exc)) from exc
    if not normalized:
        raise PolicyPreviewValidationError("at least one policy field is required")
    return normalized


def _global_effective_preview(global_policy: dict[str, Any]) -> dict[str, Any]:
    repository = _PreviewRepository(global_policy)
    resolver = PolicyResolver(repository)
    return {
        "p2p": _resolved_policy_dict(resolver.resolve_chat_policy(None, "p2p")),
        "unknown_group": _resolved_policy_dict(
            resolver.resolve_chat_policy(None, "group")
        ),
        "default_chat": _resolved_policy_dict(resolver.resolve_chat_policy(None, None)),
    }


def _effective_for_group_fallback(
    global_policy: dict[str, Any], chat_id: str
) -> dict[str, Any]:
    repository = _PreviewRepository(global_policy, {chat_id: None})
    resolver = PolicyResolver(repository)
    return _resolved_policy_dict(resolver.resolve_chat_policy(chat_id, "group"))


def _effective_from_chat_policy(policy: dict[str, Any]) -> dict[str, Any]:
    try:
        config = ChatPolicyConfig.model_validate(
            {key: value for key, value in policy.items() if key != "chat_id"}
        )
    except (TypeError, ValidationError) as exc:
        raise ProductPolicyInvalidError(
            "Product Policy Store chat policy is invalid."
        ) from exc
    return {
        "policy_source": "explicit_chat",
        "auto_reply": config.auto_reply,
        "bot_joined": config.bot_joined,
        "reply_identity": config.reply_identity,
        "allow_user_fallback": config.allow_user_fallback,
        "resource_download": config.resource_download,
    }


def _resolved_policy_dict(value: Any) -> dict[str, Any]:
    return asdict(value)


def _global_field_changes(
    old_policy: dict[str, Any], new_policy: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        _field_change(
            field,
            _global_field_value(old_policy, field),
            _global_field_value(new_policy, field),
        )
        for field in _GLOBAL_FIELDS
        if _global_field_value(old_policy, field)
        != _global_field_value(new_policy, field)
    ]


def _chat_field_changes(
    old_policy: dict[str, Any] | None, new_policy: dict[str, Any] | None
) -> list[dict[str, Any]]:
    return [
        _field_change(
            field,
            None if old_policy is None else old_policy.get(field),
            None if new_policy is None else new_policy.get(field),
        )
        for field in _CHAT_FIELDS
        if (None if old_policy is None else old_policy.get(field))
        != (None if new_policy is None else new_policy.get(field))
    ]


def _global_field_value(policy: dict[str, Any], field: str) -> Any:
    if field in {"p2p_auto_reply", "unknown_group_auto_reply"}:
        return (policy.get("reply_policy") or {}).get(field)
    return (policy.get("default_chat_policy") or {}).get(field)


def _field_change(field: str, before: Any, after: Any) -> dict[str, Any]:
    return {"field": field, "before": before, "after": after}


def _effective_group_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for subject in ("p2p", "unknown_group", "default_chat"):
        for change in _effective_changes(before[subject], after[subject]):
            changes.append({"subject": subject, **change})
    return changes


def _effective_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        _field_change(field, before.get(field), after.get(field))
        for field in _EFFECTIVE_FIELDS
        if before.get(field) != after.get(field)
    ]


def _chat_policy_count(store: SQLiteStore) -> int:
    store.migrate()
    with store.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM chat_policies").fetchone()
    if row is None:
        return 0
    return int(row["count"])
