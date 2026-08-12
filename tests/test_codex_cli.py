from __future__ import annotations

import json
import subprocess
from pathlib import Path

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.codex import (
    TASK_SESSION_DEVELOPER_INSTRUCTIONS,
    CodexCliClient,
)
from feishu_shadow_agent.config import CodexConfig, ReplyPostprocessConfig
from feishu_shadow_agent.prompt import TaskRouterOutput


def _write_last_message(argv: list[str], payload: dict[str, object] | str) -> None:
    output_path = Path(argv[argv.index("--output-last-message") + 1])
    text = payload if isinstance(payload, str) else json.dumps(payload)
    output_path.write_text(text, encoding="utf-8")


def test_codex_cli_builds_default_read_only_exec_command_with_model_reasoning_and_cwd() -> (
    None
):
    client = CodexCliClient(
        config=CodexConfig(
            path="/bin/codex", model="gpt-5.6-luna", reasoning_effort="xhigh"
        ),
        cwd="/repo",
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
        "-c",
        'model_reasoning_effort="xhigh"',
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
        "gpt-5.6-luna",
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


def test_codex_cli_invokes_explicit_skills_once_for_new_task_session(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "support"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: docmate\ndescription: docs\n---\n\n# DocMate\nRead references/faq.json.\n",
        encoding="utf-8",
    )
    prompts: list[str] = []

    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        prompts.append(stdin or "")
        if "task input" in (stdin or ""):
            payload = {
                "task_label": "task",
                "answerability": "no_reply",
                "proposed_reply": "",
                "reply_target_message_id": None,
                "watch_action": "close",
            }
        else:
            payload = {
                "route": "ignore",
                "target_task_id": None,
                "reason": "done",
            }
        _write_last_message(argv, payload)
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout='{"type":"thread.started","thread_id":"thread_1"}\n',
        )

    client = CodexCliClient(
        config=CodexConfig(path="codex", skills=["docmate"]),
        cwd=tmp_path,
        runner=runner,
    )

    router = client.structured_output("judge input", output_model=TaskRouterOutput)
    session = client.task_session("task input")
    resumed = client.task_session("follow-up task input", session_id="thread_1")

    assert router.ok and session.ok and resumed.ok
    assert prompts[0] == "judge input"
    assert prompts[1].endswith("\n\n$docmate")
    assert not any("skills.config" in argument for argument in session.argv)
    assert prompts[1].startswith("task input")
    assert "Read references/faq.json." not in prompts[1]
    assert prompts[2] == "follow-up task input"
    assert client.requested_skill_names() == ["docmate"]


def test_codex_cli_applies_silent_instruction_only_to_task_sessions() -> None:
    commands: list[list[str]] = []

    def runner(
        argv: list[str], timeout: int | None, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        commands.append(argv)
        if stdin == "judge input":
            payload = {
                "route": "ignore",
                "target_task_id": None,
                "reason": "done",
            }
        elif stdin == "task input":
            payload = {
                "task_label": "task",
                "answerability": "no_reply",
                "proposed_reply": "",
                "reply_target_message_id": None,
                "watch_action": "close",
            }
        else:
            payload = {
                "answerability": "no_reply",
                "proposed_reply": "",
                "reply_target_message_id": None,
                "watch_action": "close",
            }
        _write_last_message(argv, payload)
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout='{"type":"thread.started","thread_id":"thread_1"}\n',
        )

    client = CodexCliClient(config=CodexConfig(path="codex"), runner=runner)

    router = client.structured_output("judge input", output_model=TaskRouterOutput)
    initial = client.task_session("task input")
    resumed = client.task_session("follow-up task input", session_id="thread_1")

    assert router.ok and initial.ok and resumed.ok
    override = "developer_instructions=" + json.dumps(
        TASK_SESSION_DEVELOPER_INSTRUCTIONS
    )
    assert override not in commands[0]
    for command in commands[1:]:
        assert command[command.index("-c") + 1] == override
        assert command.index("-c") < command.index("exec")


def test_codex_cli_shows_non_native_paths_only_in_new_task_session(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "external" / "support skill(test)"
    prompts: list[str] = []

    def runner(
        argv: list[str], timeout: int, stdin: str | None, cwd: Path | None
    ) -> AgentRunResult:
        prompts.append(stdin or "")
        _write_last_message(
            argv,
            {
                "task_label": "task",
                "answerability": "no_reply",
                "proposed_reply": "",
                "reply_target_message_id": None,
                "watch_action": "close",
            },
        )
        return AgentRunResult(argv=argv, exit_code=0)

    client = CodexCliClient(
        config=CodexConfig(path="codex"),
        explicit_context_paths=[skill_dir],
        cwd=tmp_path,
        runner=runner,
    )

    result = client.task_session("task input")
    resumed = client.task_session("follow-up task input", session_id="thread-1")

    assert result.ok and resumed.ok
    assert prompts[0].endswith(
        "可用的非原生 skills（仅提供路径，尚未加载；需要时先读取对应路径并按其中说明执行）：\n"
        f"- `{skill_dir}`"
    )
    assert prompts[1] == "follow-up task input"
    assert not any("skills.config" in argument for argument in result.argv)


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


def test_codex_cli_normalizes_partial_byte_output_on_timeout(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["codex"],
            timeout=60,
            output=b'{"type":"thread.started","thread_id":"thread_1"}\n',
            stderr=b"partial stderr",
        )

    monkeypatch.setattr("feishu_shadow_agent.codex.subprocess.run", timeout)
    client = CodexCliClient(config=CodexConfig(path="codex", timeout_seconds=60))

    result = client.task_session("prompt")

    assert not result.ok
    assert result.timed_out is True
    assert result.session_id == "thread_1"
    assert result.stdout.startswith("{")
    assert result.stderr == "partial stderr"
    assert result.error == "command timed out after 60s"


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
