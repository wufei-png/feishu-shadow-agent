from __future__ import annotations

import json
from pathlib import Path

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.claude_code import ClaudeCodeCliClient
from feishu_shadow_agent.config import ClaudeCodeConfig, ReplyPostprocessConfig


def _result_envelope(
    payload: dict[str, object] | str,
    *,
    session_id: str = "4db745f0-9fd8-4b0c-8369-947445873db2",
) -> str:
    structured = payload if isinstance(payload, dict) else None
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": payload if isinstance(payload, str) else json.dumps(payload),
            "session_id": session_id,
            "structured_output": structured,
        }
    )


def test_claude_code_builds_default_read_only_command() -> None:
    client = ClaudeCodeCliClient(
        config=ClaudeCodeConfig(path="/bin/claude", model="sonnet"), cwd="/repo"
    )

    argv = client.build_print_command(output_schema={"type": "object"})

    assert argv[:5] == [
        "/bin/claude",
        "-p",
        "--output-format",
        "json",
        "--json-schema",
    ]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--tools") + 1] == ("Read,Grep,Glob,LS,WebFetch,WebSearch")
    assert argv[argv.index("--allowedTools") + 1] == (
        "Read,Grep,Glob,LS,WebFetch,WebSearch"
    )
    assert "--safe-mode" in argv
    assert argv[argv.index("--setting-sources") + 1] == "local"
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--add-dir") + 1] == "/repo"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "Bash" not in argv


def test_claude_code_maps_full_access_to_explicit_bypass() -> None:
    client = ClaudeCodeCliClient(
        config=ClaudeCodeConfig(path="/bin/claude"), tool_permissions="full_access"
    )

    argv = client.build_print_command(output_schema={"type": "object"})

    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--tools") + 1] == "default"
    assert "--allowedTools" not in argv


def test_claude_code_can_use_native_config_and_auto_context() -> None:
    client = ClaudeCodeCliClient(
        config=ClaudeCodeConfig(path="/bin/claude"),
        config_scope="native",
        auto_context="enabled",
    )

    argv = client.build_print_command(output_schema={"type": "object"})

    assert "--setting-sources" not in argv
    assert "--safe-mode" not in argv


def test_claude_code_builds_resume_command_for_followup_session() -> None:
    client = ClaudeCodeCliClient(config=ClaudeCodeConfig(path="/bin/claude"))

    argv = client.build_print_command(
        output_schema={"type": "object"},
        session_id="4db745f0-9fd8-4b0c-8369-947445873db2",
    )

    assert argv[-2:] == ["--resume", "4db745f0-9fd8-4b0c-8369-947445873db2"]


def test_claude_code_parses_structured_output_and_session_id() -> None:
    seen: dict[str, object] = {}

    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        seen["stdin"] = stdin
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        seen["required"] = schema["required"]
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout=_result_envelope(
                {
                    "route": "ignore",
                    "target_task_id": None,
                    "reason": "not relevant",
                },
                session_id="session_1",
            ),
        )

    client = ClaudeCodeCliClient(config=ClaudeCodeConfig(path="claude"), runner=runner)

    result = client.task_router("prompt")

    assert result.ok
    assert seen["stdin"] == "prompt"
    assert set(seen["required"]) == {"route", "target_task_id", "reason"}
    assert result.session_id == "session_1"
    assert result.json_data["route"] == "ignore"
    assert result.backend_provider == "claude_code"


def test_claude_code_falls_back_to_result_json_string() -> None:
    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout=_result_envelope(
                '{"answerability":"no_reply","proposed_reply":"","reply_target_message_id":null,"watch_action":"close"}'
            ),
        )

    client = ClaudeCodeCliClient(config=ClaudeCodeConfig(path="claude"), runner=runner)

    result = client.task_session(
        "prompt", session_id="4db745f0-9fd8-4b0c-8369-947445873db2"
    )

    assert result.ok
    assert result.json_data["answerability"] == "no_reply"


def test_claude_code_rejects_non_json_result_string() -> None:
    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout=_result_envelope("not json", session_id="session_1"),
        )

    client = ClaudeCodeCliClient(config=ClaudeCodeConfig(path="claude"), runner=runner)

    result = client.task_session("prompt")

    assert not result.ok
    assert result.session_id == "session_1"
    assert "not valid JSON" in (result.error or "")


def test_reply_postprocess_uses_read_only_policy_and_postprocess_model() -> None:
    seen: list[list[str]] = []

    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        seen.append(argv)
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout=_result_envelope({"status": "ok", "final_reply": "done"}),
        )

    client = ClaudeCodeCliClient(
        config=ClaudeCodeConfig(path="/bin/claude", model="main-model"),
        tool_permissions="full_access",
        reply_postprocess=ReplyPostprocessConfig(
            max_turns=5, model="style-model", owner_style={"enabled": True}
        ),
        runner=runner,
    )

    result = client.reply_postprocess("prompt")

    argv = seen[0]
    assert result.ok
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--dangerously-skip-permissions" not in argv
    assert "--resume" not in argv
    assert argv[argv.index("--model") + 1] == "style-model"
