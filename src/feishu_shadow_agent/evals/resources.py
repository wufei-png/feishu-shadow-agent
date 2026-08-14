from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, NoReturn

from ..types import LarkCliResult
from .artifacts import EvalError
from .cases import LoadedEvalCase, resource_fixture_path


class EvalResourceClient:
    """Offline Feishu client that exposes only declared resource fixtures."""

    def __init__(self, *, case: LoadedEvalCase, resource_base_dir: Path):
        self.case = case
        self.resource_base_dir = resource_base_dir
        self._fixtures = {
            (item.message_id, item.file_key, item.resource_type): item
            for item in getattr(case.scenario, "resources", [])
        }

    def download_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        output: str,
    ) -> LarkCliResult:
        key = (message_id, file_key, resource_type)
        fixture = self._fixtures.get(key)
        if fixture is None:
            raise EvalError(f"undeclared eval resource requested: {key}")
        destination = self.resource_base_dir / output
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resource_fixture_path(self.case.directory, fixture), destination)
        return LarkCliResult(
            argv=["eval-resource", message_id, file_key, resource_type, output],
            exit_code=0,
            json_data={"offline_fixture": True, "output": output},
        )

    def version(self) -> NoReturn:
        return self._network_forbidden("version")

    def auth_status(self, *, verify: bool = True) -> NoReturn:
        return self._network_forbidden("auth_status")

    def owner_message(self, **kwargs: Any) -> NoReturn:
        return self._network_forbidden("owner_message")

    def owner_card(self, **kwargs: Any) -> NoReturn:
        return self._network_forbidden("owner_card")

    def reply_message(self, **kwargs: Any) -> NoReturn:
        return self._network_forbidden("reply_message")

    def get_messages(self, **kwargs: Any) -> NoReturn:
        return self._network_forbidden("get_messages")

    def search_messages(self, **kwargs: Any) -> NoReturn:
        return self._network_forbidden("search_messages")

    def search_owner_messages(self, **kwargs: Any) -> NoReturn:
        return self._network_forbidden("search_owner_messages")

    def list_chat_messages(self, **kwargs: Any) -> NoReturn:
        return self._network_forbidden("list_chat_messages")

    def list_p2p_messages(self, **kwargs: Any) -> NoReturn:
        return self._network_forbidden("list_p2p_messages")

    def list_thread_messages(self, **kwargs: Any) -> NoReturn:
        return self._network_forbidden("list_thread_messages")

    def _network_forbidden(self, operation: str) -> NoReturn:
        raise EvalError(f"full-chain eval forbids Feishu operation: {operation}")
