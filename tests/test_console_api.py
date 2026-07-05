from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

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


def _seed_legacy_0001_store_without_agent_working_dir(store: SQLiteStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    migration = resources.files("feishu_shadow_agent.store").joinpath(
        "migrations/0001_foundation.sql"
    )
    with store.connect() as conn:
        conn.executescript(migration.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ("0001_foundation", "now"),
        )
        conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, chat_type, root_message_id, task_label, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t_legacy",
                "watching",
                "oc_legacy",
                "p2p",
                "om_legacy",
                "legacy",
                "now",
                "now",
            ),
        )


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


def _client(
    tmp_path: Path,
    *,
    token: str = "test-token",
    host: str = "127.0.0.1",
    readback_marker: Any | None = None,
) -> TestClient:
    config = ConfigService().load(_write_config(tmp_path))
    app = create_console_app(
        loaded_config=config,
        store=_store(tmp_path),
        token=token,
        host=host,
        port=8765,
        static_dir=_static_dir(tmp_path),
        readback_marker=readback_marker,
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


def test_console_command_defaults_to_loopback_and_prints_token_url(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config = _write_config(tmp_path)
    called: dict[str, object] = {}

    monkeypatch.setattr(
        "feishu_shadow_agent.cli.console_static_ready", lambda static_dir: True
    )
    monkeypatch.setattr(
        "feishu_shadow_agent.cli.generate_console_token", lambda: "fixed-token"
    )
    monkeypatch.setattr(
        "feishu_shadow_agent.cli._run_console_server",
        lambda app, *, host, port: called.update(
            {"app": app, "host": host, "port": port}
        ),
    )

    assert main(["console", "--config", str(config)]) == 0

    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8765
    assert "http://127.0.0.1:8765/?token=fixed-token" in capsys.readouterr().out


def test_console_command_migrates_legacy_store_before_starting_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    _seed_legacy_0001_store_without_agent_working_dir(store)
    called: dict[str, object] = {}

    monkeypatch.setattr(
        "feishu_shadow_agent.cli.console_static_ready", lambda static_dir: True
    )
    monkeypatch.setattr(
        "feishu_shadow_agent.cli.generate_console_token", lambda: "fixed-token"
    )
    monkeypatch.setattr(
        "feishu_shadow_agent.cli._run_console_server",
        lambda app, *, host, port: called.update(
            {"app": app, "host": host, "port": port}
        ),
    )

    assert main(["console", "--config", str(config)]) == 0

    assert called["host"] == "127.0.0.1"
    assert "Operator Console:" in capsys.readouterr().out
    with store.connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        task = conn.execute("SELECT short_id, agent_working_dir FROM tasks").fetchone()
    assert "agent_working_dir" in columns
    assert task["short_id"] == "t_legacy"
    assert task["agent_working_dir"] is None


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


def test_dashboard_redacts_failed_approval_command_body(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO approval_commands(message_id, command, status, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "cmd_dashboard_secret",
                "/send t_dashboard highly sensitive final reply",
                "failed",
                json.dumps({"error": "active send action already exists"}),
                "2026-07-01T10:00:00+08:00",
                "2026-07-01T10:00:00+08:00",
            ),
        )

    response = client.get("/api/dashboard", headers=_auth())
    payload = response.json()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert response.status_code == 200
    assert "highly sensitive final reply" not in serialized
    assert payload["recent_errors"][0]["message"] == "/send t_dashboard"
    assert payload["failed_approval_commands"][0]["label"] == "/send t_dashboard"


def test_dashboard_redacts_health_warning_paths(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO health_checks(run_id, check_name, severity, status, message, details_json, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                "hermes",
                "warning",
                "failed",
                "Hermes failed at /tmp/secret/hermes.log",
                "{}",
                "2026-07-01T10:00:00+08:00",
            ),
        )

    response = client.get("/api/dashboard", headers=_auth())
    payload = response.json()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert response.status_code == 200
    assert "/tmp/secret" not in serialized
    assert payload["recent_health_warnings"][0]["message"] == "Hermes failed at [path]"


def test_settings_catalog_and_runtime_routes_are_readonly_product_maps(
    tmp_path: Path,
) -> None:
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


def test_incomplete_static_assets_fail_ready_check_and_do_not_fall_back_to_index(
    tmp_path: Path,
) -> None:
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


def test_message_detail_reports_store_unavailable_separately_from_missing_message(
    tmp_path: Path,
) -> None:
    unavailable_client = _client(tmp_path / "unavailable")
    unavailable = unavailable_client.get(
        "/api/messages/om_missing/detail", headers=_auth()
    )

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
            (
                "t_msg",
                "watching",
                "oc_1",
                "p2p",
                "om_1",
                "message detail",
                "now",
                "now",
            ),
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
        payload={
            "reply_target_message_id": "om_1",
            "text": "reply",
            "identity": "user",
        },
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


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/approvals"),
        ("GET", "/api/approvals/a_missing"),
        ("GET", "/api/tasks"),
        ("GET", "/api/tasks/t_missing"),
        ("GET", "/api/dispatch/actions"),
        ("GET", "/api/dispatch/actions/1"),
        ("POST", "/api/approvals/a_missing/approve"),
        ("POST", "/api/approvals/a_missing/reject"),
        ("POST", "/api/tasks/t_missing/send"),
        ("POST", "/api/tasks/t_missing/close"),
        ("POST", "/api/tasks/t_missing/reopen"),
        ("POST", "/api/maintenance/expire-approvals"),
        ("POST", "/api/dispatch/actions/1/retry"),
        ("POST", "/api/dispatch/actions/1/cancel"),
        ("POST", "/api/dispatch/actions/1/mark-sent"),
        ("GET", "/api/policy/status"),
        ("GET", "/api/policy/audits"),
        ("POST", "/api/policy/import-config"),
        ("PATCH", "/api/policy/global"),
        ("PATCH", "/api/policy/chats/oc_policy"),
        ("DELETE", "/api/policy/chats/oc_policy"),
        ("GET", "/api/health/issues"),
    ],
)
def test_core_console_routes_require_token(
    tmp_path: Path, method: str, path: str
) -> None:
    client = _client(tmp_path)

    response = client.request(method, path, json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_core_read_routes_use_operator_query_filters_and_details(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    task_id = _seed_task_with_message(store)
    approval_id = store.create_send_reply_approval(
        task_id=task_id,
        preview="draft reply",
        payload={
            "reply_target_message_id": "om_1",
            "text": "draft reply",
            "identity": "user",
        },
        approval_timeout_hours=None,
    )
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={
            "reply_target_message_id": "om_1",
            "text": "draft reply",
            "identity": "user",
        },
        approval_id=approval_id,
    )
    assert action_id is not None
    with store.connect() as conn:
        approval_short_id = conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()["short_id"]
        task_short_id = conn.execute(
            "SELECT short_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["short_id"]

    approvals = client.get(
        "/api/approvals?status=pending&limit=10&offset=0", headers=_auth()
    )
    approval_detail = client.get(f"/api/approvals/{approval_short_id}", headers=_auth())
    tasks = client.get("/api/tasks?status=watching&chat_id=oc_1", headers=_auth())
    task_detail = client.get(f"/api/tasks/{task_short_id}", headers=_auth())
    actions = client.get("/api/dispatch/actions?status=pending", headers=_auth())
    action_detail = client.get(f"/api/dispatch/actions/{action_id}", headers=_auth())

    assert approvals.status_code == 200
    assert approvals.json()[0]["approval_id"] == approval_short_id
    assert "payload" not in approvals.json()[0]
    assert approval_detail.status_code == 200
    assert approval_detail.json()["payload"]["text"] == "draft reply"
    assert tasks.status_code == 200
    assert tasks.json()[0]["task_id"] == task_short_id
    assert task_detail.status_code == 200
    assert task_detail.json()["recent_messages"][0]["message_id"] == "om_1"
    assert actions.status_code == 200
    assert actions.json()[0]["action_id"] == action_id
    assert action_detail.status_code == 200
    assert action_detail.json()["action"]["payload"]["text"] == "draft reply"


@pytest.mark.parametrize(
    "path",
    [
        "/api/approvals?status=waiting_approval",
        "/api/tasks?status=waiting_approval",
        "/api/dispatch/actions?status=unknown",
        "/api/approvals?limit=101",
        "/api/tasks?offset=-1",
    ],
)
def test_core_read_routes_return_standard_validation_errors(
    tmp_path: Path, path: str
) -> None:
    client = _client(tmp_path)

    response = client.get(path, headers=_auth())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_approval_and_task_command_routes_return_command_results(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    task_id = _seed_task_with_message(store, task_short_id="t_api_cmd")
    approval_id = store.create_send_reply_approval(
        task_id=task_id,
        preview="approve me",
        payload={
            "reply_target_message_id": "om_1",
            "text": "approve me",
            "identity": "user",
        },
        approval_timeout_hours=None,
    )
    with store.connect() as conn:
        approval_short_id = conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()["short_id"]

    approve = client.post(
        f"/api/approvals/{approval_short_id}/approve",
        headers=_auth(),
        json={"reason": "reviewed", "command_id": "cmd_approve_api"},
    )
    send = client.post(
        "/api/tasks/t_api_cmd/send",
        headers=_auth(),
        json={
            "final_reply": "operator final",
            "reason": "manual close",
            "command_id": "cmd_send_api",
        },
    )
    invalid_send = client.post(
        "/api/tasks/t_api_cmd/send", headers=_auth(), json={"final_reply": "   "}
    )

    assert approve.status_code == 200
    approve_payload = approve.json()
    assert approve_payload["status"] == "applied"
    assert approve_payload["command"] == "approval.approve"
    assert approve_payload["actor"] == "local_console"
    assert approve_payload["reason"] == "reviewed"

    _seed_task_with_message(store, task_short_id="t_api_send", message_id="om_send")
    assert send.status_code == 200
    send_payload = send.json()
    assert send_payload["status"] == "conflict"
    conflict = send_payload
    send = client.post(
        "/api/tasks/t_api_send/send",
        headers=_auth(),
        json={
            "final_reply": "operator final",
            "reason": "manual close",
            "command_id": "cmd_send_api_2",
        },
    )
    close = client.post(
        "/api/tasks/t_api_send/close", headers=_auth(), json={"reason": "done"}
    )
    reopen = client.post(
        "/api/tasks/t_api_send/reopen", headers=_auth(), json={"reason": "again"}
    )
    send_payload = send.json()
    assert conflict["status"] == "conflict"
    assert send_payload["status"] == "applied"
    assert send_payload["command"] == "approval.send"
    assert send_payload["actor"] == "local_console"
    assert send_payload["result"]["approval_command_status"] == "applied"
    assert close.status_code == 200
    assert close.json()["status"] == "applied"
    assert close.json()["command"] == "task.close"
    assert close.json()["actor"] == "local_console"
    assert reopen.status_code == 200
    assert reopen.json()["status"] == "applied"
    assert reopen.json()["command"] == "task.reopen"
    assert invalid_send.status_code == 400
    assert invalid_send.json()["error"]["code"] == "validation_failed"


def test_maintenance_and_dispatch_command_routes_return_command_results(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    task_id = _seed_task_with_message(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={
            "reply_target_message_id": "om_1",
            "text": "recover me",
            "identity": "user",
        },
    )
    assert action_id is not None
    store.finish_action(action_id, status="failed", result={"error_stage": "send"})

    retry = client.post(
        f"/api/dispatch/actions/{action_id}/retry",
        headers=_auth(),
        json={"reason": "try again"},
    )
    cancel = client.post(
        f"/api/dispatch/actions/{action_id}/cancel",
        headers=_auth(),
        json={"reason": "stop"},
    )
    expire = client.post(
        "/api/maintenance/expire-approvals", headers=_auth(), json={"reason": "sweep"}
    )

    assert retry.status_code == 200
    assert retry.json()["status"] == "applied"
    assert retry.json()["command"] == "dispatch.retry"
    assert retry.json()["actor"] == "local_console"
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "applied"
    assert cancel.json()["command"] == "dispatch.cancel"
    assert expire.status_code == 200
    assert expire.json()["command"] == "maintenance.expire_approvals"
    assert expire.json()["actor"] == "local_console"


def test_dispatch_mark_sent_route_uses_readback_marker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _seed_task_with_message(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={
            "reply_target_message_id": "om_1",
            "text": "sent already",
            "identity": "user",
        },
    )
    assert action_id is not None
    store.finish_action(
        action_id, status="failed_needs_review", result={"error_stage": "send"}
    )

    class FakeReadbackMarker:
        calls: list[dict[str, Any]]

        def __init__(self) -> None:
            self.calls = []

        def mark_action_sent_after_readback(
            self,
            action_id: int,
            *,
            sent_message_id: str,
            run_id: str,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "action_id": action_id,
                    "sent_message_id": sent_message_id,
                    "run_id": run_id,
                }
            )
            store.mark_action_sent_after_evidence(
                action_id,
                sent_message_id=sent_message_id,
                result={
                    "readback": {"ok": True, "message_id": sent_message_id},
                    "warnings": [],
                },
                run_id=run_id,
            )
            return {
                "status": "sent",
                "action_id": action_id,
                "sent_message_id": sent_message_id,
            }

    marker = FakeReadbackMarker()
    client = _client(tmp_path, readback_marker=marker)

    response = client.post(
        f"/api/dispatch/actions/{action_id}/mark-sent",
        headers=_auth(),
        json={"sent_message_id": "om_sent", "reason": "verified in Feishu"},
    )
    missing = client.post(
        f"/api/dispatch/actions/{action_id}/mark-sent", headers=_auth(), json={}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "applied"
    assert payload["command"] == "dispatch.mark_sent"
    assert payload["actor"] == "local_console"
    assert payload["reason"] == "verified in Feishu"
    assert payload["result"]["sent_message_id"] == "om_sent"
    assert marker.calls[0]["action_id"] == action_id
    assert marker.calls[0]["sent_message_id"] == "om_sent"
    assert store.get_action(action_id).status == "sent"  # type: ignore[union-attr]
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "validation_failed"


def test_policy_routes_use_command_facade_and_settings_runtime_read_model(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)

    initial_status = client.get("/api/policy/status", headers=_auth())
    imported = client.post(
        "/api/policy/import-config",
        headers=_auth(),
        json={"reason": "seed local console policy"},
    )
    updated_global = client.patch(
        "/api/policy/global",
        headers=_auth(),
        json={"p2p_auto_reply": False, "reason": "pause p2p"},
    )
    updated_chat = client.patch(
        "/api/policy/chats/oc_console",
        headers=_auth(),
        json={
            "name": "Console group",
            "auto_reply": True,
            "bot_joined": True,
            "reply_identity": "bot",
            "allow_user_fallback": False,
            "resource_download": False,
            "reason": "open console test chat",
        },
    )
    audits = client.get("/api/policy/audits?limit=10&scope=chat", headers=_auth())
    runtime = client.get("/api/settings/runtime", headers=_auth())

    assert initial_status.status_code == 200
    assert initial_status.json()["initialized"] is False
    assert imported.status_code == 200
    imported_payload = imported.json()
    assert imported_payload["status"] == "applied"
    assert imported_payload["command"] == "policy.import_config"
    assert imported_payload["actor"] == "local_console"
    assert imported_payload["reason"] == "seed local console policy"
    assert imported_payload["policy_import_diff"]["status"] == "matches"
    assert updated_global.status_code == 200
    global_payload = updated_global.json()
    assert global_payload["command"] == "policy.update_global"
    assert global_payload["actor"] == "local_console"
    assert global_payload["new_policy"]["reply_policy"]["p2p_auto_reply"] is False
    assert updated_chat.status_code == 200
    chat_payload = updated_chat.json()
    assert chat_payload["command"] == "policy.update_chat"
    assert chat_payload["target"] == {"type": "chat_policy", "chat_id": "oc_console"}
    assert chat_payload["new_policy"]["reply_identity"] == "bot"
    assert audits.status_code == 200
    assert audits.json()[0]["actor"] == "local_console"
    assert audits.json()[0]["reason"] == "open console test chat"
    assert runtime.status_code == 200
    runtime_payload = runtime.json()
    assert runtime_payload["global_policy"]["reply_policy"]["p2p_auto_reply"] is False
    assert runtime_payload["values"]["policy.global.p2p_auto_reply"] is False
    assert runtime_payload["chat_policies"][0]["chat_id"] == "oc_console"
    assert runtime_payload["policy_audit_history"][0]["scope"] == "chat"
    assert store.get_chat_product_policy("oc_console")["auto_reply"] is True


def test_console_policy_delete_chat_removes_override_and_updates_runtime(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    imported = client.post("/api/policy/import-config", headers=_auth(), json={})
    created = client.patch(
        "/api/policy/chats/oc_console",
        headers=_auth(),
        json={"auto_reply": True, "reason": "open chat"},
    )

    deleted = client.request(
        "DELETE",
        "/api/policy/chats/oc_console",
        headers=_auth(),
        json={"reason": "remove override"},
    )
    audits = client.get(
        "/api/policy/audits?limit=1&scope=chat&policy_key=chat:oc_console",
        headers=_auth(),
    )
    runtime = client.get("/api/settings/runtime", headers=_auth())

    assert imported.status_code == 200
    assert created.status_code == 200
    assert deleted.status_code == 200
    payload = deleted.json()
    assert payload["status"] == "applied"
    assert payload["command"] == "policy.delete_chat"
    assert payload["old_policy"]["auto_reply"] is True
    assert payload["new_policy"] is None
    assert payload["audit_count"] == 1
    assert audits.status_code == 200
    assert audits.json()[0]["new_summary"] == {}
    assert runtime.status_code == 200
    assert runtime.json()["chat_policies"] == []
    assert store.get_chat_product_policy("oc_console") is None


def test_policy_routes_return_standard_validation_errors(tmp_path: Path) -> None:
    client = _client(tmp_path)

    invalid_audit_limit = client.get("/api/policy/audits?limit=101", headers=_auth())
    invalid_patch_field = client.patch(
        "/api/policy/global",
        headers=_auth(),
        json={"config_drift": True},
    )

    assert invalid_audit_limit.status_code == 400
    assert invalid_audit_limit.json()["error"]["code"] == "validation_failed"
    assert invalid_patch_field.status_code == 400
    assert invalid_patch_field.json()["error"]["code"] == "validation_failed"


def test_health_issues_route_returns_normalized_store_issue(tmp_path: Path) -> None:
    client = _client(tmp_path / "missing")

    response = client.get("/api/health/issues", headers=_auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["highest_severity"] == "critical"
    assert payload["summary"]["open_issue_count"] == 1
    assert payload["runtime"]["store"]["status"] == "missing"
    assert payload["issues"][0]["category"] == "store"
    assert payload["issues"][0]["severity"] == "critical"
    assert "agent.sqlite3" not in payload["issues"][0]["detail"]


def test_health_issues_route_validates_limit(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/health/issues?limit=101", headers=_auth())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_dispatch_mark_sent_route_reports_marker_construction_failure(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    task_id = _seed_task_with_message(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={
            "reply_target_message_id": "om_1",
            "text": "sent already",
            "identity": "user",
        },
    )
    assert action_id is not None
    store.finish_action(
        action_id, status="failed_needs_review", result={"error_stage": "send"}
    )
    monkeypatch.setattr(
        "feishu_shadow_agent.console_api._build_dispatch_readback_marker",
        lambda **_: (_ for _ in ()).throw(OSError("log path unavailable")),
    )
    client = _client(tmp_path)

    response = client.post(
        f"/api/dispatch/actions/{action_id}/mark-sent",
        headers=_auth(),
        json={"sent_message_id": "om_sent", "reason": "verified in Feishu"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["command"] == "dispatch.mark_sent"
    assert payload["actor"] == "local_console"
    assert payload["changed"] is False
    assert "readback marker unavailable" in payload["result"]["error"]
    assert store.get_action(action_id).status == "failed_needs_review"  # type: ignore[union-attr]


def _seed_task_with_message(
    store: SQLiteStore,
    *,
    task_short_id: str = "t_api",
    message_id: str = "om_1",
) -> int:
    store.migrate()
    with store.connect() as conn:
        task_id = conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, chat_type, root_message_id, task_label, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_short_id,
                "watching",
                "oc_1",
                "p2p",
                message_id,
                "api task",
                "2026-06-22T10:00:00+08:00",
                "2026-06-22T10:00:00+08:00",
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO messages(message_id, chat_id, chat_type, sender_id, sender_role, sent_at, text, raw_json, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                "oc_1",
                "p2p",
                "ou_external",
                "external_user_message",
                "2026-06-22T10:00:00+08:00",
                "hello",
                json.dumps({"message_id": message_id}),
                "2026-06-22T10:00:00+08:00",
            ),
        )
        conn.execute(
            "INSERT INTO task_messages(task_id, message_id, role, created_at) VALUES (?, ?, ?, ?)",
            (task_id, message_id, "root", "2026-06-22T10:00:00+08:00"),
        )
    return int(task_id)


def _message_detail_state(store: SQLiteStore, action_id: int) -> dict[str, object]:
    with store.connect() as conn:
        approval = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE short_id = ?", ("a_msg",)
        ).fetchone()
        action = conn.execute(
            "SELECT status, result_json FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        attempts = conn.execute(
            "SELECT COUNT(*) AS count FROM dispatch_attempts WHERE action_id = ?",
            (action_id,),
        ).fetchone()
    return {
        "approval_status": approval["status"],
        "approval_resolved_at": approval["resolved_at"],
        "action_status": action["status"],
        "action_result_json": action["result_json"],
        "attempt_count": attempts["count"],
    }
