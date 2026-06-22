from __future__ import annotations

from typing import Protocol

from ..types import LarkCliResult


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
