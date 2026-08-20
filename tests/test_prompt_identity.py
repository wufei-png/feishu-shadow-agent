from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from feishu_shadow_agent.evals.backend_trace import merge_prompt_versions
from feishu_shadow_agent.prompt_identity import PROMPT_VERSIONS, identify_prompt
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def test_identify_prompt_uses_catalog_version_and_sha256() -> None:
    identity = identify_prompt("router", "prompt body")

    assert identity.kind == "router"
    assert identity.version == PROMPT_VERSIONS["router"]
    assert identity.version == "v2"
    assert identity.sha256 == hashlib.sha256(b"prompt body").hexdigest()


def test_prompt_catalog_bumps_rewritten_kinds_to_v2() -> None:
    assert PROMPT_VERSIONS == {
        "router": "v2",
        "task_session": "v2",
        "reply_postprocess": "v2",
        "owner_style_refresh": "v2",
        "ingress_judge": "v2",
        "semantic_judge": "v2",
        "structured_output": "v1",
    }


def test_identify_prompt_rejects_unregistered_prompt_kind() -> None:
    with pytest.raises(ValueError, match="unknown prompt kind"):
        identify_prompt("not_registered", "prompt body")


def test_prompt_versions_report_mixed_versions_explicitly() -> None:
    assert merge_prompt_versions([{"task_session": "v1"}]) == {"task_session": "v1"}
    assert merge_prompt_versions([{"task_session": "v1"}, {"task_session": "v2"}]) == {
        "task_session": "mixed:v1,v2"
    }


def test_agent_audit_persists_prompt_identity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    identity = identify_prompt("task_session", "prompt body")

    store.record_agent_audit(
        backend_provider="test",
        request_type=identity.kind,
        task_id=None,
        agent_session_id=None,
        input_message_ids=[],
        input_resource_ids=[],
        prompt_version=identity.version,
        prompt_hash=identity.sha256,
    )

    with store.connect() as conn:
        row = conn.execute(
            "SELECT prompt_version, prompt_hash FROM agent_audits"
        ).fetchone()
    assert row["prompt_version"] == identity.version
    assert row["prompt_hash"] == identity.sha256
