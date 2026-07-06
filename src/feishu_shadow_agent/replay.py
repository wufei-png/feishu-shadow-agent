from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .dispatcher import Dispatcher
from .feishu.lark_cli import LarkCliClient
from .jsonl import JSONLLogger
from .store.sqlite_store import SQLiteStore
from .types import new_run_id


def replay_message_dry_run(
    *,
    loaded_config: LoadedConfig,
    store: SQLiteStore,
    message_id: str,
    lark_client_factory: Callable[..., Any] = LarkCliClient,
) -> dict[str, Any] | None:
    """Explain one message and preview related pending dispatch without touching the real DB."""
    client = lark_client_factory(
        path=loaded_config.config.lark_cli.path,
        timeout_seconds=loaded_config.config.lark_cli.timeout_seconds,
        cwd=loaded_config.base_dir,
    )
    previews = []
    with tempfile.TemporaryDirectory(prefix="feishu-shadow-agent-replay-") as tmp:
        temp_db = Path(tmp) / "agent.sqlite3"
        if store.path.exists():
            shutil.copy2(store.path, temp_db)
        temp_store = SQLiteStore(temp_db)
        temp_store.migrate()
        summary = temp_store.replay_summary(message_id)
        if summary is None:
            return None
        related_pending_action_ids = [
            action["id"]
            for action in summary["actions"]
            if action.get("status") == "pending"
            and action.get("kind") in {"send_reply", "owner_notification"}
        ]
        dispatcher = Dispatcher(
            store=temp_store,
            feishu_client=client,
            config=loaded_config.config,
            logger=JSONLLogger(Path(tmp) / "replay.jsonl"),
        )
        run_id = new_run_id("replay")
        for action_id in related_pending_action_ids:
            preview = dispatcher.preview_action(action_id, run_id=run_id)
            if preview is not None:
                previews.append(preview)
    return {
        "message_id": message_id,
        "state": summary,
        "dispatch_preview": {
            "processed": len(previews),
            "actions": previews,
        },
        "mutated_real_db": False,
    }
