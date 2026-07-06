from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from ..config import AppConfig, ChatPolicyConfig
from ..policy import (
    PolicyResolver,
    ProductPolicyInvalidError,
    ProductPolicyMissingError,
)
from ..settings_catalog import CONFIG_VALUE_PATHS
from ..store.sqlite_store import PRODUCT_POLICY_KEY
from .common import (
    _coerce_limit,
    _coerce_offset,
    _loads_json_object,
    _ReadStoreUnavailable,
    _row_dict,
)


class PolicyQuery:
    """Read-only query slice for Product Policy Store and settings runtime views."""

    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        policy_import_source: AppConfig | None = None,
    ):
        self._connect = connect
        self.policy_import_source = policy_import_source
        self.policy_resolver = PolicyResolver(_ReadOnlyProductPolicyRepository(self))

    def policy_status(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                global_policy = conn.execute(
                    "SELECT policy_json, updated_at FROM product_policies WHERE key = ?",
                    (PRODUCT_POLICY_KEY,),
                ).fetchone()
                chat_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM chat_policies"
                ).fetchone()
                diff = self._policy_import_diff(conn)
        except _ReadStoreUnavailable:
            return {
                "initialized": False,
                "global_policy_updated_at": None,
                "chat_policy_count": 0,
                "policy_import_diff": self._missing_store_policy_import_diff(),
            }
        return {
            "initialized": global_policy is not None,
            "global_policy_updated_at": None
            if global_policy is None
            else global_policy["updated_at"],
            "chat_policy_count": 0 if chat_count is None else int(chat_count["count"]),
            "policy_import_diff": diff,
        }

    def settings_runtime(self, config: AppConfig) -> dict[str, Any]:
        policy_status = self.policy_status()
        global_policy = self._get_product_policy()
        chat_policies = self._list_chat_product_policies()
        return {
            "values": _settings_values(
                config, policy_status=policy_status, global_policy=global_policy
            ),
            "global_policy": global_policy,
            "chat_policies": chat_policies,
            "policy_status": policy_status,
            "policy_audit_history": self.policy_audit_history(limit=20),
        }

    def effective_policy_summary(
        self, chat_id: str | None, chat_type: str | None
    ) -> dict[str, Any]:
        if not chat_id:
            return _empty_effective_policy("unknown_chat")
        try:
            policy = self.policy_resolver.resolve_chat_policy(chat_id, chat_type)
        except ProductPolicyMissingError as exc:
            return _empty_effective_policy("uninitialized", error=str(exc))
        except ProductPolicyInvalidError as exc:
            return _empty_effective_policy("invalid", error=str(exc))
        return {
            "policy_source": policy.policy_source,
            "auto_reply": policy.auto_reply,
            "bot_joined": policy.bot_joined,
            "reply_identity": policy.reply_identity,
            "allow_user_fallback": policy.allow_user_fallback,
            "resource_download": policy.resource_download,
        }

    def policy_audit_history(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        scope: str | None = None,
        policy_key: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if scope is not None:
            where.append("scope = ?")
            params.append(scope)
        if policy_key is not None:
            where.append("policy_key = ?")
            params.append(policy_key)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([_coerce_limit(limit), _coerce_offset(offset)])
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT id, scope, policy_key, actor, reason, created_at, old_json, new_json
                    FROM policy_audits
                    {where_sql}
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        return [_policy_audit_dto(row) for row in rows]

    def validate_policy_store(self) -> None:
        self.policy_resolver.resolve_chat_policy(None, None)

    def _get_product_policy(self) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT policy_json FROM product_policies WHERE key = ?",
                    (PRODUCT_POLICY_KEY,),
                ).fetchone()
        except _ReadStoreUnavailable:
            return None
        return None if row is None else _loads_json_object(row["policy_json"])

    def _get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                           allow_user_fallback, resource_download
                    FROM chat_policies
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                ).fetchone()
        except _ReadStoreUnavailable:
            return None
        return None if row is None else _chat_policy_from_row(row)

    def _list_chat_product_policies(self, *, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                           allow_user_fallback, resource_download, updated_at
                    FROM chat_policies
                    ORDER BY chat_id
                    LIMIT ?
                    """,
                    (_coerce_limit(limit),),
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        return [_chat_policy_runtime_dto(row) for row in rows]

    def _policy_import_diff(self, conn: sqlite3.Connection) -> dict[str, Any]:
        if self.policy_import_source is None:
            return {
                "status": "unknown",
                "message": "No Policy Import Source was provided for comparison.",
            }
        source_global = _global_policy_from_import_source(self.policy_import_source)
        global_row = conn.execute(
            "SELECT policy_json FROM product_policies WHERE key = ?",
            (PRODUCT_POLICY_KEY,),
        ).fetchone()
        missing_global = global_row is None
        changed_global = False
        if global_row is not None:
            changed_global = (
                _loads_json_object(global_row["policy_json"]) != source_global
            )

        missing_chats: list[str] = []
        changed_chats: list[str] = []
        for chat_id, chat_config in sorted(self.policy_import_source.chats.items()):
            source_chat = _chat_policy_from_import_source(chat_id, chat_config)
            row = conn.execute(
                """
                SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                       allow_user_fallback, resource_download
                FROM chat_policies
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            if row is None:
                missing_chats.append(chat_id)
            elif _chat_policy_from_row(row) != source_chat:
                changed_chats.append(chat_id)

        if (
            not missing_global
            and not changed_global
            and not missing_chats
            and not changed_chats
        ):
            return {
                "status": "matches",
                "message": "Policy Import Source matches Product Policy Store for global policy and config-listed chats.",
                "missing_global": False,
                "changed_global": False,
                "missing_chats": [],
                "changed_chats": [],
            }
        return {
            "status": "differs",
            "message": (
                "Policy Import Source differs from Product Policy Store; import-config would insert missing "
                "rows and import-config --replace would update changed rows."
            ),
            "missing_global": missing_global,
            "changed_global": changed_global,
            "missing_chats": missing_chats,
            "changed_chats": changed_chats,
        }

    def _missing_store_policy_import_diff(self) -> dict[str, Any]:
        if self.policy_import_source is None:
            return {
                "status": "unknown",
                "message": "No Policy Import Source was provided for comparison.",
            }
        return {
            "status": "differs",
            "message": (
                "Policy Import Source differs from Product Policy Store because no initialized store "
                "is available to compare."
            ),
            "missing_global": True,
            "changed_global": False,
            "missing_chats": sorted(self.policy_import_source.chats),
            "changed_chats": [],
        }


class _ReadOnlyProductPolicyRepository:
    def __init__(self, query: PolicyQuery):
        self.query = query

    def get_product_policy(self) -> dict[str, Any] | None:
        return self.query._get_product_policy()

    def get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None:
        return self.query._get_chat_product_policy(chat_id)


def _policy_audit_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "id": data["id"],
        "scope": data["scope"],
        "policy_key": data["policy_key"],
        "actor": data["actor"],
        "reason": data["reason"],
        "created_at": data["created_at"],
        "old_summary": _policy_summary(_loads_json_object(data["old_json"])),
        "new_summary": _policy_summary(_loads_json_object(data["new_json"])),
    }


def _policy_summary(policy: dict[str, Any]) -> dict[str, Any]:
    if not policy:
        return {}
    allowed = (
        "chat_id",
        "name",
        "auto_reply",
        "bot_joined",
        "reply_identity",
        "allow_user_fallback",
        "resource_download",
        "reply_policy",
        "default_chat_policy",
    )
    return {key: policy[key] for key in allowed if key in policy}


def _empty_effective_policy(
    policy_source: str, *, error: str | None = None
) -> dict[str, Any]:
    dto: dict[str, Any] = {
        "policy_source": policy_source,
        "auto_reply": None,
        "bot_joined": None,
        "reply_identity": None,
        "allow_user_fallback": None,
        "resource_download": None,
    }
    if error is not None:
        dto["error"] = error
    return dto


def _global_policy_from_import_source(config: AppConfig) -> dict[str, Any]:
    default_chat_policy = ChatPolicyConfig().model_dump(mode="json")
    return {
        "reply_policy": config.reply_policy.model_dump(mode="json"),
        "default_chat_policy": {
            key: default_chat_policy[key]
            for key in (
                "bot_joined",
                "reply_identity",
                "allow_user_fallback",
                "resource_download",
            )
        },
    }


def _chat_policy_from_import_source(
    chat_id: str, config: ChatPolicyConfig
) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    return {"chat_id": chat_id, **data}


def _chat_policy_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "chat_id": row["chat_id"],
        "name": row["name"] or "",
        "auto_reply": bool(row["auto_reply"]),
        "bot_joined": bool(row["bot_joined"]),
        "reply_identity": row["reply_identity"],
        "allow_user_fallback": bool(row["allow_user_fallback"]),
        "resource_download": bool(row["resource_download"]),
    }


def _chat_policy_runtime_dto(row: sqlite3.Row) -> dict[str, Any]:
    return _chat_policy_from_row(row) | {"updated_at": row["updated_at"]}


def _settings_values(
    config: AppConfig,
    *,
    policy_status: dict[str, Any],
    global_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    values = {
        key: _config_path_value(config, path)
        for key, path in CONFIG_VALUE_PATHS.items()
    }
    values["policy.status.initialized"] = policy_status["initialized"]
    values["policy.status.import_diff"] = policy_status["policy_import_diff"]
    values["policy.audit.history"] = None
    values["policy.import_config"] = {"available": True}

    reply_policy = _nested_dict(global_policy, "reply_policy")
    default_chat_policy = _nested_dict(global_policy, "default_chat_policy")
    values["policy.global.p2p_auto_reply"] = reply_policy.get("p2p_auto_reply")
    values["policy.global.unknown_group_auto_reply"] = reply_policy.get(
        "unknown_group_auto_reply"
    )
    values["policy.global.default_bot_joined"] = default_chat_policy.get("bot_joined")
    values["policy.global.default_reply_identity"] = default_chat_policy.get(
        "reply_identity"
    )
    values["policy.global.default_allow_user_fallback"] = default_chat_policy.get(
        "allow_user_fallback"
    )
    values["policy.global.default_resource_download"] = default_chat_policy.get(
        "resource_download"
    )
    return values


def _config_path_value(config: AppConfig, path: str) -> Any:
    current: Any = config
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part)
    return current


def _nested_dict(value: dict[str, Any] | None, key: str) -> dict[str, Any]:
    if value is None:
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, dict) else {}
