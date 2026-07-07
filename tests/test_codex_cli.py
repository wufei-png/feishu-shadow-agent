from __future__ import annotations

import json
from pathlib import Path

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.codex import CodexCliClient
from feishu_shadow_agent.config import CodexConfig, ReplyPostprocessConfig


def _write_last_message(argv: list[str], payload: dict[str, object] | str) -> None:
    output_path = Path(argv[argv.index("--output-last-message") + 1])
    text = payload if isinstance(payload, str) else json.dumps(payload)
    output_path.write_text(text, encoding="utf-8")


def test_codex_cli_builds_default_read_only_exec_command_with_model_and_cwd() -> None:
    client = CodexCliClient(
        config=CodexConfig(path="/bin/codex", model="gpt-5"), cwd="/repo"
    )

    argv = client.build_exec_command(
        output_schema_path="/tmp/schema.json",
        output_path="/tmp/output.json",
    )

    assert argv == [
        "/bin/codex",
        "--search",
        "--ask-for-approval",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "--cd",
        "/repo",
        "--json",
        "--output-schema",
        "/tmp/schema.json",
        "--output-last-message",
        "/tmp/output.json",
        "--model",
        "gpt-5",
        "-",
    ]


def test_codex_cli_maps_full_access_to_explicit_bypass() -> None:
    client = CodexCliClient(
        config=CodexConfig(path="/bin/codex"), tool_permissions="full_access"
    )

    argv = client.build_exec_command(
        output_schema_path="/tmp/schema.json",
        output_path="/tmp/output.json",
    )

    assert "--search" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--sandbox" not in argv
    assert "--ask-for-approval" not in argv


def test_codex_cli_can_use_native_config_and_auto_context() -> None:
    client = CodexCliClient(
        config=CodexConfig(path="/bin/codex"),
        config_scope="native",
        auto_context="enabled",
    )

    argv = client.build_exec_command(
        output_schema_path="/tmp/schema.json",
        output_path="/tmp/output.json",
    )

    assert "--ignore-user-config" not in argv
    assert "--ignore-rules" not in argv


def test_codex_cli_builds_resume_command_for_followup_session() -> None:
    client = CodexCliClient(config=CodexConfig(path="/bin/codex"))

    argv = client.build_exec_command(
        output_schema_path="/tmp/schema.json",
        output_path="/tmp/output.json",
        session_id="019f3d87-afa5-7140-9ba2-2c92ec10de87",
    )

    assert argv[-3:] == [
        "resume",
        "019f3d87-afa5-7140-9ba2-2c92ec10de87",
        "-",
    ]


def test_codex_cli_parses_last_message_json_and_thread_id() -> None:
    seen: dict[str, object] = {}

    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        seen["stdin"] = stdin
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        seen["required"] = schema["required"]
        _write_last_message(
            argv,
            {"route": "ignore", "target_task_id": None, "reason": "not relevant"},
        )
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout='{"type":"thread.started","thread_id":"thread_1"}\n',
        )

    client = CodexCliClient(config=CodexConfig(path="codex"), runner=runner)

    result = client.task_router("prompt")

    assert result.ok
    assert seen["stdin"] == "prompt"
    assert set(seen["required"]) == {"route", "target_task_id", "reason"}
    assert result.session_id == "thread_1"
    assert result.json_data["route"] == "ignore"
    assert result.backend_provider == "codex"


def test_codex_cli_falls_back_to_agent_message_event() -> None:
    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"thread_1"}',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"answerability\\":\\"no_reply\\",\\"proposed_reply\\":\\"\\",\\"reply_target_message_id\\":null,\\"watch_action\\":\\"close\\"}"}}',
                ]
            ),
        )

    client = CodexCliClient(config=CodexConfig(path="codex"), runner=runner)

    result = client.task_session("prompt", session_id="thread_1")

    assert result.ok
    assert result.json_data["answerability"] == "no_reply"


def test_codex_cli_rejects_non_json_final_message() -> None:
    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        _write_last_message(argv, "not json")
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout='{"type":"thread.started","thread_id":"thread_1"}\n',
        )

    client = CodexCliClient(config=CodexConfig(path="codex"), runner=runner)

    result = client.task_session("prompt")

    assert not result.ok
    assert result.session_id == "thread_1"
    assert "not valid JSON" in (result.error or "")


def test_reply_postprocess_uses_read_only_policy_and_postprocess_model() -> None:
    seen: list[list[str]] = []

    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        seen.append(argv)
        _write_last_message(argv, {"status": "ok", "final_reply": "done"})
        return AgentRunResult(argv=argv, exit_code=0, stdout="")

    client = CodexCliClient(
        config=CodexConfig(path="/bin/codex", model="main-model"),
        tool_permissions="full_access",
        reply_postprocess=ReplyPostprocessConfig(
            max_turns=5, model="style-model", owner_style={"enabled": True}
        ),
        runner=runner,
    )

    result = client.reply_postprocess("prompt")

    argv = seen[0]
    assert result.ok
    assert "--ask-for-approval" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "resume" not in argv
    assert argv[argv.index("--model") + 1] == "style-model"
