from __future__ import annotations

from typing import Protocol

from ..types import LarkCliResult, MessagePage


class FeishuClient(Protocol):
    def version(self) -> LarkCliResult:
        ...

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        ...

    def owner_message(
        self,
        *,
        owner_open_id: str,
        text: str,
        idempotency_key: str,
        dry_run: bool = True,
    ) -> LarkCliResult:
        ...

    def search_messages(
        self,
        *,
        chat_type: str,
        is_at_me: bool,
        start: str | None,
        end: str | None,
        page_token: str | None = None,
        query: str = "",
        page_size: int = 50,
    ) -> MessagePage:
        ...

    def list_chat_messages(
        self,
        *,
        chat_id: str,
        start: str | None,
        end: str | None,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> MessagePage:
        ...

    def list_thread_messages(
        self,
        *,
        thread_id: str,
        page_token: str | None = None,
        page_size: int = 50,
    ) -> MessagePage:
        ...

    def download_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        output: str,
    ) -> LarkCliResult:
        ...
