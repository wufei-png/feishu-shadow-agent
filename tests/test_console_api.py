from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from feishu_shadow_agent.cli import main
from feishu_shadow_agent.config import ConfigService
from feishu_shadow_agent.console_api import console_static_ready, create_console_app
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _write_config(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.yaml"
    config.write_text(
        """
owner:
  open_id: ou_owner
  name: Owner
storage:
  sqlite_path: agent.sqlite3
logging:
  jsonl_path: agent.jsonl
lifecycle:
  approval_timeout_hours: 12
""".lstrip(),
        encoding="utf-8",
    )
    return config


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "agent.sqlite3")


def _static_dir(tmp_path: Path) -> Path:
    static_dir = tmp_path / "console_static"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text(
        '<!doctype html><div id="root"></div><script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("console.log('console');", encoding="utf-8")
    return static_dir


def _client(tmp_path: Path, *, token: str = "test-token", host: str = "127.0.0.1") -> TestClient:
    config = ConfigService().load(_write_config(tmp_path))
    app = create_console_app(
        loaded_config=config,
        store=_store(tmp_path),
        token=token,
        host=host,
        port=8765,
        static_dir=_static_dir(tmp_path),
    )
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _auth(token: str = "test-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_console_help_shows_console_command(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["console", "--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--host" in output
    assert "--port" in output


def test_console_command_defaults_to_loopback_and_prints_token_url(tmp_path: Path, monkeypatch, capsys) -> None:
    config = _write_config(tmp_path)
    called: dict[str, object] = {}

    monkeypatch.setattr("feishu_shadow_agent.cli.console_static_ready", lambda static_dir: True)
    monkeypatch.setattr("feishu_shadow_agent.cli.generate_console_token", lambda: "fixed-token")
    monkeypatch.setattr(
        "feishu_shadow_agent.cli._run_console_server",
        lambda app, *, host, port: called.update({"app": app, "host": host, "port": port}),
    )

    assert main(["console", "--config", str(config)]) == 0

    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8765
    assert "http://127.0.0.1:8765/?token=fixed-token" in capsys.readouterr().out


def test_dashboard_rejects_missing_and_invalid_token(tmp_path: Path) -> None:
    client = _client(tmp_path)

    missing = client.get("/api/dashboard")
    invalid = client.get("/api/dashboard", headers=_auth("wrong-token"))

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "unauthorized"


def test_host_validation_rejects_unexpected_host(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/dashboard", headers={**_auth(), "Host": "example.com"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin_or_host"


def test_unknown_api_routes_still_require_token(tmp_path: Path) -> None:
    client = _client(tmp_path)

    missing_token = client.get("/api/not-found")
    valid_token = client.get("/api/not-found", headers=_auth())

    assert missing_token.status_code == 401
    assert missing_token.json()["error"]["code"] == "unauthorized"
    assert valid_token.status_code == 404
    assert valid_token.json()["error"]["code"] == "not_found"


def test_dashboard_returns_operator_query_dto_with_valid_token(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/dashboard", headers=_auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["daemon_liveness"]["status"] == "not_started"
    assert payload["policy_status"]["initialized"] is False
    assert "policy_audits" not in payload


def test_settings_catalog_and_runtime_routes_are_readonly_product_maps(tmp_path: Path) -> None:
    client = _client(tmp_path)

    catalog = client.get("/api/settings/catalog", headers=_auth()).json()
    runtime = client.get("/api/settings/runtime", headers=_auth()).json()

    keys = {entry["key"] for entry in catalog["entries"]}
    assert "policy.global.p2p_auto_reply" in keys
    assert "lifecycle.approval_timeout_hours" in keys
    assert "debug.save_full_agent_io" in keys
    assert runtime["values"]["lifecycle.approval_timeout_hours"] == 12
    assert runtime["values"]["policy.status.initialized"] is False
    assert runtime["policy_status"]["policy_import_diff"]["status"] == "differs"


def test_static_renderer_assets_are_served(tmp_path: Path) -> None:
    client = _client(tmp_path)

    index = client.get("/")
    asset = client.get("/assets/app.js")

    assert console_static_ready(_static_dir(tmp_path))
    assert index.status_code == 200
    assert '<div id="root"></div>' in index.text
    assert asset.status_code == 200
    assert "console.log" in asset.text


def test_incomplete_static_assets_fail_ready_check_and_do_not_fall_back_to_index(tmp_path: Path) -> None:
    static_dir = tmp_path / "broken_static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        '<!doctype html><div id="root"></div><script type="module" src="/assets/missing.js"></script>',
        encoding="utf-8",
    )
    config = ConfigService().load(_write_config(tmp_path))
    app = create_console_app(
        loaded_config=config,
        store=_store(tmp_path),
        token="test-token",
        host="127.0.0.1",
        port=8765,
        static_dir=static_dir,
    )
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    response = client.get("/assets/missing.js")

    assert console_static_ready(static_dir) is False
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_message_detail_reports_store_unavailable_separately_from_missing_message(tmp_path: Path) -> None:
    unavailable_client = _client(tmp_path / "unavailable")
    unavailable = unavailable_client.get("/api/messages/om_missing/detail", headers=_auth())

    ready_tmp = tmp_path / "ready"
    ready_tmp.mkdir()
    _store(ready_tmp).migrate()
    ready_client = _client(ready_tmp)
    missing = ready_client.get("/api/messages/om_missing/detail", headers=_auth())

    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "store_unavailable"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_message_detail_api_is_service_backed_and_read_only(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    store.migrate()
    with store.connect() as conn:
        task_id = conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, chat_type, root_message_id, task_label, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("t_msg", "watching", "oc_1", "p2p", "om_1", "message detail", "now", "now"),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO messages(message_id, chat_id, chat_type, sender_id, sender_role, sent_at, text, raw_json, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "om_1",
                "oc_1",
                "p2p",
                "ou_external",
                "external_user_message",
                "2026-06-22T10:00:00+08:00",
                "hello",
                json.dumps({"message_id": "om_1"}),
                "now",
            ),
        )
        conn.execute(
            "INSERT INTO task_messages(task_id, message_id, role, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "om_1", "root", "now"),
        )
        conn.execute(
            """
            INSERT INTO routing_audits(message_id, task_id, route, route_reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("om_1", task_id, "new_task", "new task", "now"),
        )
        conn.execute(
            """
            INSERT INTO approvals(short_id, task_id, kind, status, payload_json, preview, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a_msg",
                task_id,
                "send_reply",
                "pending",
                json.dumps({"reply_target_message_id": "om_1", "text": "reply"}),
                "reply",
                "now",
                None,
            ),
        )
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={"reply_target_message_id": "om_1", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    before = _message_detail_state(store, action_id)

    response = client.get("/api/messages/om_1/detail", headers=_auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"]["message_id"] == "om_1"
    assert payload["task_ids"] == [task_id]
    assert payload["task_summaries"][0]["task_id"] == "t_msg"
    assert payload["routing_audits"][0]["route"] == "new_task"
    assert payload["approvals"][0]["approval_id"] == "a_msg"
    assert payload["actions"][0]["action_id"] == action_id
    assert payload["recorded_dispatch_outcomes"][0]["attempts"] == []
    assert payload["recommended_actions"] == ["review_pending_approvals"]
    assert _message_detail_state(store, action_id) == before


def _message_detail_state(store: SQLiteStore, action_id: int) -> dict[str, object]:
    with store.connect() as conn:
        approval = conn.execute("SELECT status, resolved_at FROM approvals WHERE short_id = ?", ("a_msg",)).fetchone()
        action = conn.execute("SELECT status, result_json FROM actions WHERE id = ?", (action_id,)).fetchone()
        attempts = conn.execute("SELECT COUNT(*) AS count FROM dispatch_attempts WHERE action_id = ?", (action_id,)).fetchone()
    return {
        "approval_status": approval["status"],
        "approval_resolved_at": approval["resolved_at"],
        "action_status": action["status"],
        "action_result_json": action["result_json"],
        "attempt_count": attempts["count"],
    }
