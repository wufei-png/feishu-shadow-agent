from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from ..config import AppConfig, ChatPolicyConfig, CoworkerConfig, ReplyPolicyConfig
from ._common import cursor_lastrowid as _cursor_lastrowid
from ._common import json_row_dict as _json_row_dict
from ._common import loads_json_object as _loads_json_object

PRODUCT_POLICY_KEY = "reply_policy"
COWORKER_POLICY_KEY = "ai_coworker"


def stored_coworker_defaults(policy: dict[str, Any]) -> dict[str, Any]:
    return {"send_mode": "project", "memory_maintenance": True, **policy}


class PolicyStore:
    """Policy persistence using the caller's SQLite lifecycle and transactions."""

    def __init__(
        self,
        *,
        initialize: Callable[[], None],
        connect: Callable[[], sqlite3.Connection],
        clock: Callable[[], str],
    ) -> None:
        self.initialize = initialize
        self.connect = connect
        self.clock = clock

    def get_product_policy(
        self, key: str = PRODUCT_POLICY_KEY
    ) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        policy = json.loads(row["policy_json"])
        if key == COWORKER_POLICY_KEY:
            policy = stored_coworker_defaults(policy)
        return policy

    def get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                       allow_user_fallback, resource_download
                FROM chat_policies
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        return None if row is None else _chat_policy_from_row(row)

    def list_chat_policy_activations(self) -> list[dict[str, Any]]:
        """Return current chat policies with their latest auditable activation."""
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT cp.chat_id, pa.id AS activation_id, pa.created_at AS activated_at
                FROM chat_policies cp
                LEFT JOIN policy_audits pa ON pa.id = (
                  SELECT id FROM policy_audits
                  WHERE scope = 'chat' AND policy_key = 'chat:' || cp.chat_id
                    AND old_json IS NULL AND new_json IS NOT NULL
                  ORDER BY id DESC LIMIT 1
                )
                ORDER BY cp.chat_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def is_group_policy_held(self, kind: str, item_id: int) -> bool:
        self.initialize()
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM group_policy_holds WHERE kind = ? AND item_id = ?",
                    (kind, item_id),
                ).fetchone()
                is not None
            )

    def product_policy_initialization_probe(self) -> dict[str, Any]:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM product_policies WHERE key = ?",
                (PRODUCT_POLICY_KEY,),
            ).fetchone()
        missing = [] if row is not None else [f"global:{PRODUCT_POLICY_KEY}"]
        return {
            "initialized": not missing,
            "missing": missing,
        }

    def list_policy_audits(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, scope, policy_key, actor, old_json, new_json, reason, created_at
                FROM policy_audits
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_json_row_dict(row, "old_json", "new_json") for row in rows]

    def import_product_policy_from_config(
        self,
        config: AppConfig,
        *,
        replace: bool = False,
        used_defaults: bool = False,
        actor: str = "import_config",
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        audit_reason = reason or (
            "policy import-config --replace" if replace else "policy import-config"
        )
        result: dict[str, Any] = {
            "status": "imported",
            "mode": "replace" if replace else "fill_missing",
            "used_defaults": used_defaults,
            "inserted": {"global": [], "chats": []},
            "skipped": {"global": [], "chats": []},
            "replaced": {"global": [], "chats": []},
            "audit_count": 0,
        }
        global_policy = _global_product_policy_from_config(config)
        with self.connect() as conn:
            conn.execute("BEGIN EXCLUSIVE")
            now = self.clock()
            existing_global = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (PRODUCT_POLICY_KEY,),
            ).fetchone()
            if existing_global is None:
                conn.execute(
                    """
                    INSERT INTO product_policies(key, policy_json, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (PRODUCT_POLICY_KEY, _policy_json(global_policy), now),
                )
                self._record_policy_audit_locked(
                    conn,
                    scope="global",
                    policy_key=PRODUCT_POLICY_KEY,
                    old_policy=None,
                    new_policy=global_policy,
                    actor=actor,
                    reason=audit_reason,
                    now=now,
                )
                result["inserted"]["global"].append(PRODUCT_POLICY_KEY)
                result["audit_count"] += 1
            elif replace:
                old_policy = json.loads(existing_global["policy_json"])
                conn.execute(
                    """
                    UPDATE product_policies
                    SET policy_json = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (_policy_json(global_policy), now, PRODUCT_POLICY_KEY),
                )
                self._record_policy_audit_locked(
                    conn,
                    scope="global",
                    policy_key=PRODUCT_POLICY_KEY,
                    old_policy=old_policy,
                    new_policy=global_policy,
                    actor=actor,
                    reason=audit_reason,
                    now=now,
                )
                result["replaced"]["global"].append(PRODUCT_POLICY_KEY)
                result["audit_count"] += 1
            else:
                result["skipped"]["global"].append(PRODUCT_POLICY_KEY)

            for chat_id, chat_config in sorted(config.chats.items()):
                chat_policy = _chat_policy_from_config(chat_id, chat_config)
                existing_chat = conn.execute(
                    """
                    SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                           allow_user_fallback, resource_download
                    FROM chat_policies
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                ).fetchone()
                if existing_chat is None:
                    self._insert_chat_policy_locked(conn, chat_policy, now=now)
                    self._record_policy_audit_locked(
                        conn,
                        scope="chat",
                        policy_key=f"chat:{chat_id}",
                        old_policy=None,
                        new_policy=chat_policy,
                        actor=actor,
                        reason=audit_reason,
                        now=now,
                    )
                    result["inserted"]["chats"].append(chat_id)
                    result["audit_count"] += 1
                elif replace:
                    old_policy = _chat_policy_from_row(existing_chat)
                    self._update_chat_policy_locked(conn, chat_policy, now=now)
                    self._record_policy_audit_locked(
                        conn,
                        scope="chat",
                        policy_key=f"chat:{chat_id}",
                        old_policy=old_policy,
                        new_policy=chat_policy,
                        actor=actor,
                        reason=audit_reason,
                        now=now,
                    )
                    result["replaced"]["chats"].append(chat_id)
                    result["audit_count"] += 1
                else:
                    result["skipped"]["chats"].append(chat_id)
            coworker = config.coworker.model_dump(mode="json")
            existing_coworker = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (COWORKER_POLICY_KEY,),
            ).fetchone()
            if existing_coworker is None and config.coworker.enabled:
                conn.execute(
                    "INSERT INTO product_policies(key, policy_json, updated_at) VALUES (?, ?, ?)",
                    (COWORKER_POLICY_KEY, _policy_json(coworker), now),
                )
                self._record_policy_audit_locked(
                    conn,
                    scope="global",
                    policy_key=COWORKER_POLICY_KEY,
                    old_policy=None,
                    new_policy=coworker,
                    actor=actor,
                    reason=audit_reason,
                    now=now,
                )
                result["inserted"]["global"].append(COWORKER_POLICY_KEY)
                result["audit_count"] += 1
            elif existing_coworker is not None and replace:
                old_coworker = json.loads(existing_coworker["policy_json"])
                conn.execute(
                    "UPDATE product_policies SET policy_json = ?, updated_at = ? WHERE key = ?",
                    (_policy_json(coworker), now, COWORKER_POLICY_KEY),
                )
                self._record_policy_audit_locked(
                    conn,
                    scope="global",
                    policy_key=COWORKER_POLICY_KEY,
                    old_policy=old_coworker,
                    new_policy=coworker,
                    actor=actor,
                    reason=audit_reason,
                    now=now,
                )
                result["replaced"]["global"].append(COWORKER_POLICY_KEY)
                result["audit_count"] += 1
            elif existing_coworker is not None:
                result["skipped"]["global"].append(COWORKER_POLICY_KEY)
        result["initialization"] = self.product_policy_initialization_probe()
        return result

    def update_coworker_policy(
        self, changes: dict[str, Any], *, actor: str, reason: str | None = None
    ) -> dict[str, Any]:
        allowed = {
            "enabled",
            "chat_id",
            "provider",
            "send_mode",
            "trigger_mode",
            "history_source",
            "memory_maintenance",
            "memory_maintenance_time",
        }
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("unsupported coworker policy change")
        self.initialize()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (COWORKER_POLICY_KEY,),
            ).fetchone()
            if row is None:
                raise ValueError("coworker policy is not imported")
            old = stored_coworker_defaults(json.loads(row["policy_json"]))
            current = CoworkerConfig.model_validate(old).model_dump(mode="json")
            updated = CoworkerConfig.model_validate(current | changes).model_dump(
                mode="json"
            )
            if current == updated:
                return {"changed": False, "policy": current, "audit_id": None}
            now = self.clock()
            conn.execute(
                "UPDATE product_policies SET policy_json = ?, updated_at = ? WHERE key = ?",
                (_policy_json(updated), now, COWORKER_POLICY_KEY),
            )
            audit_id = self._record_policy_audit_locked(
                conn,
                scope="global",
                policy_key=COWORKER_POLICY_KEY,
                old_policy=old,
                new_policy=updated,
                actor=actor,
                reason=reason or "coworker policy update",
                now=now,
            )
        return {"changed": True, "policy": updated, "audit_id": audit_id}

    def update_product_policy(
        self,
        policy: dict[str, Any],
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = self.clock()
        new_policy = _normalize_global_product_policy(policy)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (PRODUCT_POLICY_KEY,),
            ).fetchone()
            old_policy = (
                None if existing is None else json.loads(existing["policy_json"])
            )
            if old_policy == new_policy:
                return {
                    "scope": "global",
                    "policy_key": PRODUCT_POLICY_KEY,
                    "changed": False,
                    "old_policy": old_policy,
                    "new_policy": new_policy,
                    "audit_id": None,
                }
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO product_policies(key, policy_json, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (PRODUCT_POLICY_KEY, _policy_json(new_policy), now),
                )
            else:
                conn.execute(
                    """
                    UPDATE product_policies
                    SET policy_json = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (_policy_json(new_policy), now, PRODUCT_POLICY_KEY),
                )
            audit_id = self._record_policy_audit_locked(
                conn,
                scope="global",
                policy_key=PRODUCT_POLICY_KEY,
                old_policy=old_policy,
                new_policy=new_policy,
                actor=actor,
                reason=reason or "policy update-global",
                now=now,
            )
        return {
            "scope": "global",
            "policy_key": PRODUCT_POLICY_KEY,
            "changed": True,
            "old_policy": old_policy,
            "new_policy": new_policy,
            "audit_id": audit_id,
        }

    def upsert_chat_product_policy(
        self,
        policy: dict[str, Any],
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        new_policy = _normalize_chat_product_policy(policy)
        chat_id = str(new_policy["chat_id"])
        with self.connect() as conn:
            conn.execute("BEGIN EXCLUSIVE")
            now = self.clock()
            existing = conn.execute(
                """
                SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                       allow_user_fallback, resource_download
                FROM chat_policies
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            old_policy = None if existing is None else _chat_policy_from_row(existing)
            if old_policy == new_policy:
                return {
                    "scope": "chat",
                    "policy_key": f"chat:{chat_id}",
                    "changed": False,
                    "old_policy": old_policy,
                    "new_policy": new_policy,
                    "audit_id": None,
                }
            if existing is None:
                self._insert_chat_policy_locked(conn, new_policy, now=now)
            else:
                self._update_chat_policy_locked(conn, new_policy, now=now)
            audit_id = self._record_policy_audit_locked(
                conn,
                scope="chat",
                policy_key=f"chat:{chat_id}",
                old_policy=old_policy,
                new_policy=new_policy,
                actor=actor,
                reason=reason or "policy update-chat",
                now=now,
            )
        return {
            "scope": "chat",
            "policy_key": f"chat:{chat_id}",
            "changed": True,
            "old_policy": old_policy,
            "new_policy": new_policy,
            "audit_id": audit_id,
        }

    def delete_chat_product_policy(
        self,
        chat_id: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = self.clock()
        normalized_chat_id = chat_id.strip()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                       allow_user_fallback, resource_download
                FROM chat_policies
                WHERE chat_id = ?
                """,
                (normalized_chat_id,),
            ).fetchone()
            old_policy = None if existing is None else _chat_policy_from_row(existing)
            if old_policy is None:
                return {
                    "scope": "chat",
                    "policy_key": f"chat:{normalized_chat_id}",
                    "changed": False,
                    "old_policy": None,
                    "new_policy": None,
                    "audit_id": None,
                }
            _hold_group_policy_items_locked(conn, normalized_chat_id, now=now)
            conn.execute(
                "DELETE FROM chat_policies WHERE chat_id = ?",
                (normalized_chat_id,),
            )
            audit_id = self._record_policy_audit_locked(
                conn,
                scope="chat",
                policy_key=f"chat:{normalized_chat_id}",
                old_policy=old_policy,
                new_policy=None,
                actor=actor,
                reason=reason or "policy delete-chat",
                now=now,
            )
        return {
            "scope": "chat",
            "policy_key": f"chat:{normalized_chat_id}",
            "changed": True,
            "old_policy": old_policy,
            "new_policy": None,
            "audit_id": audit_id,
        }

    def replace_product_policy_snapshot(
        self,
        global_policy: dict[str, Any],
        chats: list[dict[str, Any]],
        *,
        source: str,
        coworker_policy: dict[str, Any] | None = None,
    ) -> None:
        """Atomically replace policy only, retaining and holding other local state."""
        normalized_global = _normalize_global_product_policy(global_policy)
        normalized_chats = [_normalize_chat_product_policy(chat) for chat in chats]
        if coworker_policy is not None:
            coworker_policy = stored_coworker_defaults(coworker_policy)
        normalized_coworker = (
            None
            if coworker_policy is None
            else CoworkerConfig.model_validate(coworker_policy).model_dump(mode="json")
        )
        incoming = {str(chat["chat_id"]): chat for chat in normalized_chats}
        self.initialize()
        actor = "dry_run_policy_sync"
        reason = "production policy snapshot"
        with self.connect() as conn:
            conn.execute("BEGIN EXCLUSIVE")
            now = self.clock()
            global_row = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (PRODUCT_POLICY_KEY,),
            ).fetchone()
            old_global = (
                None if global_row is None else json.loads(global_row["policy_json"])
            )
            if old_global != normalized_global:
                conn.execute(
                    "INSERT INTO product_policies(key, policy_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET policy_json = excluded.policy_json, updated_at = excluded.updated_at",
                    (PRODUCT_POLICY_KEY, _policy_json(normalized_global), now),
                )
                self._record_policy_audit_locked(
                    conn,
                    scope="global",
                    policy_key=PRODUCT_POLICY_KEY,
                    old_policy=old_global,
                    new_policy=normalized_global,
                    actor=actor,
                    reason=reason,
                    now=now,
                )
            coworker_row = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (COWORKER_POLICY_KEY,),
            ).fetchone()
            old_coworker = (
                None
                if coworker_row is None
                else json.loads(coworker_row["policy_json"])
            )
            if old_coworker != normalized_coworker:
                if normalized_coworker is None:
                    conn.execute(
                        "DELETE FROM product_policies WHERE key = ?",
                        (COWORKER_POLICY_KEY,),
                    )
                else:
                    conn.execute(
                        "INSERT INTO product_policies(key, policy_json, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET policy_json = excluded.policy_json, updated_at = excluded.updated_at",
                        (COWORKER_POLICY_KEY, _policy_json(normalized_coworker), now),
                    )
                self._record_policy_audit_locked(
                    conn,
                    scope="global",
                    policy_key=COWORKER_POLICY_KEY,
                    old_policy=old_coworker,
                    new_policy=normalized_coworker,
                    actor=actor,
                    reason=reason,
                    now=now,
                )
            rows = conn.execute(
                "SELECT chat_id, name, auto_reply, bot_joined, reply_identity, allow_user_fallback, resource_download FROM chat_policies"
            ).fetchall()
            existing = {str(row["chat_id"]): _chat_policy_from_row(row) for row in rows}
            for chat_id in sorted(existing.keys() - incoming.keys()):
                _hold_group_policy_items_locked(conn, chat_id, now=now)
                conn.execute("DELETE FROM chat_policies WHERE chat_id = ?", (chat_id,))
                self._record_policy_audit_locked(
                    conn,
                    scope="chat",
                    policy_key=f"chat:{chat_id}",
                    old_policy=existing[chat_id],
                    new_policy=None,
                    actor=actor,
                    reason=reason,
                    now=now,
                )
            for chat_id, policy in sorted(incoming.items()):
                old_policy = existing.get(chat_id)
                if old_policy == policy:
                    continue
                if old_policy is None:
                    self._insert_chat_policy_locked(conn, policy, now=now)
                else:
                    self._update_chat_policy_locked(conn, policy, now=now)
                self._record_policy_audit_locked(
                    conn,
                    scope="chat",
                    policy_key=f"chat:{chat_id}",
                    old_policy=old_policy,
                    new_policy=policy,
                    actor=actor,
                    reason=reason,
                    now=now,
                )
            conn.execute(
                "INSERT INTO checkpoints(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                ("dry_run.policy_initialized", _policy_json({"source": source}), now),
            )

    def _insert_chat_policy_locked(
        self,
        conn: sqlite3.Connection,
        policy: dict[str, Any],
        *,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO chat_policies(
              chat_id, name, auto_reply, bot_joined, reply_identity,
              allow_user_fallback, resource_download, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy["chat_id"],
                policy["name"],
                int(policy["auto_reply"]),
                int(policy["bot_joined"]),
                policy["reply_identity"],
                int(policy["allow_user_fallback"]),
                int(policy["resource_download"]),
                now,
            ),
        )

    def _update_chat_policy_locked(
        self,
        conn: sqlite3.Connection,
        policy: dict[str, Any],
        *,
        now: str,
    ) -> None:
        conn.execute(
            """
            UPDATE chat_policies
            SET name = ?,
                auto_reply = ?,
                bot_joined = ?,
                reply_identity = ?,
                allow_user_fallback = ?,
                resource_download = ?,
                updated_at = ?
            WHERE chat_id = ?
            """,
            (
                policy["name"],
                int(policy["auto_reply"]),
                int(policy["bot_joined"]),
                policy["reply_identity"],
                int(policy["allow_user_fallback"]),
                int(policy["resource_download"]),
                now,
                policy["chat_id"],
            ),
        )

    def _record_policy_audit_locked(
        self,
        conn: sqlite3.Connection,
        *,
        scope: str,
        policy_key: str,
        old_policy: dict[str, Any] | None,
        new_policy: dict[str, Any] | None,
        actor: str,
        reason: str,
        now: str,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO policy_audits(
              scope, policy_key, actor, old_json, new_json, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                policy_key,
                actor,
                None if old_policy is None else _policy_json(old_policy),
                None if new_policy is None else _policy_json(new_policy),
                reason,
                now,
            ),
        )
        return _cursor_lastrowid(cursor)


def _global_product_policy_from_config(config: AppConfig) -> dict[str, Any]:
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


def _chat_policy_from_config(chat_id: str, config: ChatPolicyConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    return {"chat_id": chat_id, **data}


def _normalize_global_product_policy(policy: dict[str, Any]) -> dict[str, Any]:
    reply_policy = ReplyPolicyConfig.model_validate(
        policy.get("reply_policy")
    ).model_dump(mode="json")
    default_chat_policy = ChatPolicyConfig.model_validate(
        {
            "name": "",
            "auto_reply": False,
            **_loads_json_object(policy.get("default_chat_policy")),
        }
    ).model_dump(mode="json")
    return {
        "reply_policy": reply_policy,
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


def _normalize_chat_product_policy(policy: dict[str, Any]) -> dict[str, Any]:
    chat_id = str(policy.get("chat_id", "")).strip()
    if not chat_id:
        raise ValueError("chat_id is required")
    chat_policy = ChatPolicyConfig.model_validate(
        {key: value for key, value in policy.items() if key != "chat_id"}
    ).model_dump(mode="json")
    return {"chat_id": chat_id, **chat_policy}


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


def _policy_json(policy: dict[str, Any]) -> str:
    return json.dumps(policy, ensure_ascii=False, sort_keys=True, default=str)


def _hold_group_policy_items_locked(
    conn: sqlite3.Connection, chat_id: str, *, now: str
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO group_policy_holds(kind, item_id, chat_id, created_at)
        SELECT 'task', id, chat_id, ? FROM tasks
        WHERE chat_id = ? AND chat_type = 'group' AND status = 'watching'
        """,
        (now, chat_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO group_policy_holds(kind, item_id, chat_id, created_at)
        SELECT 'action', a.id, ?, ? FROM actions a
        LEFT JOIN tasks t ON t.id = a.task_id
        LEFT JOIN messages m ON m.message_id = a.source_message_id
        WHERE a.status IN ('pending', 'sending') AND (
          (t.chat_type = 'group' AND t.chat_id = ?)
          OR (m.chat_type = 'group' AND m.chat_id = ?)
          OR (json_extract(a.payload_json, '$.type') IN ('bot_membership_absent', 'bot_membership_recovered')
              AND json_extract(a.payload_json, '$.chat_id') = ?)
        )
        """,
        (chat_id, now, chat_id, chat_id, chat_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO group_policy_holds(kind, item_id, chat_id, created_at)
        SELECT 'retry', r.id, m.chat_id, ? FROM processing_retry_attempts r
        JOIN messages m ON m.message_id = r.message_id
        WHERE m.chat_id = ? AND m.chat_type = 'group'
          AND r.status IN ('queued', 'claimed')
        """,
        (now, chat_id),
    )
