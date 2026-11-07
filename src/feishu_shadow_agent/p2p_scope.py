from __future__ import annotations

from typing import Any, Protocol, cast

from .types import LarkCliResult, MessagePage


class P2PScopeClient(Protocol):
    def auth_status(self, *, verify: bool = True) -> LarkCliResult: ...
    def list_user_p2p_chats(self, *, page_token: str | None = None) -> MessagePage: ...


def bot_open_id_from_auth(auth_json: Any) -> str | None:
    if not isinstance(auth_json, dict):
        return None
    identities: Any = cast(dict[str, Any], auth_json).get("identities")
    if not isinstance(identities, dict):
        return None
    bot: Any = cast(dict[str, Any], identities).get("bot")
    if not isinstance(bot, dict):
        return None
    bot = cast(dict[str, Any], bot)
    for key in ("openId", "open_id", "openID", "id"):
        value: Any = bot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def list_non_bot_p2p_chats(client: P2PScopeClient) -> set[str]:
    """Enumerate every peer before allowing any private-message query."""
    auth = client.auth_status(verify=True)
    bot_id = bot_open_id_from_auth(auth.json_data) if auth.ok else None
    if not bot_id or not bot_id.startswith("ou_") or len(bot_id) <= 3:
        raise ValueError("bot open_id is unavailable")
    allowed: set[str] = set()
    peers: dict[str, tuple[str, str]] = {}
    seen_tokens: set[str] = set()
    token: str | None = None
    for _ in range(1000):
        page = client.list_user_p2p_chats(page_token=token)
        for chat in page.items:
            chat_id = chat.get("chat_id") or chat.get("chatId")
            target_id = chat.get("p2p_target_id")
            target_type = chat.get("p2p_target_type")
            if (
                not isinstance(chat_id, str)
                or not chat_id.strip()
                or not isinstance(target_id, str)
                or not target_id.strip().startswith("ou_")
                or len(target_id.strip()) <= 3
                or target_type not in {"user", "bot"}
                or chat.get("chat_mode") != "p2p"
            ):
                raise ValueError("P2P chat metadata is incomplete")
            chat_id = chat_id.strip()
            target_id = target_id.strip()
            if chat_id in peers and peers[chat_id] != (target_id, target_type):
                raise ValueError("P2P chat metadata is inconsistent")
            peers[chat_id] = (target_id, target_type)
            if target_id != bot_id and target_type != "bot":
                allowed.add(chat_id)
        if not page.has_more:
            return allowed
        token = page.next_page_token
        if not token or token in seen_tokens:
            raise ValueError("P2P chat pagination is incomplete")
        seen_tokens.add(token)
    raise ValueError("P2P chat pagination exceeded its safety limit")
