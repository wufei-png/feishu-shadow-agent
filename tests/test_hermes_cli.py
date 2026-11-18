from __future__ import annotations

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.config import HermesConfig, ReplyPostprocessConfig
from feishu_shadow_agent.hermes import HermesCliClient
from feishu_shadow_agent.prompt import TaskRouterOutput


def test_hermes_cli_builds_default_read_only_chat_command_with_model_provider_and_resume() -> (
    None
):
    config = HermesConfig(
        path="/bin/hermes",
        source="feishu-shadow-agent",
        session_max_turns=7,
        model="m",
        provider="p",
    )
    client = HermesCliClient(config=config)

    argv = client.build_chat_command(
        prompt='{"ok": true}', max_turns=7, session_id="sid_1"
    )

    assert argv == [
        "/bin/hermes",
        "chat",
        "-q",
        '{"ok": true}',
        "-Q",
        "--source",
        "feishu-shadow-agent",
        "--toolsets",
        "safe",
        "--max-turns",
        "7",
        "--ignore-user-config",
        "--ignore-rules",
        "--resume",
        "sid_1",
        "--model",
        "m",
        "--provider",
        "p",
    ]


def test_hermes_cli_maps_read_only_to_safe_toolset() -> None:
    client = HermesCliClient(
        config=HermesConfig(path="/bin/hermes"), tool_permissions="read_only"
    )

    argv = client.build_chat_command(prompt="{}", max_turns=1)

    assert "--toolsets" in argv
    assert argv[argv.index("--toolsets") + 1] == "safe"
    assert "--yolo" not in argv


def test_hermes_cli_maps_full_access_to_yolo_full_toolset() -> None:
    client = HermesCliClient(
        config=HermesConfig(path="/bin/hermes"), tool_permissions="full_access"
    )

    argv = client.build_chat_command(prompt="{}", max_turns=1)

    assert argv[argv.index("--toolsets") + 1] == "hermes-cli"
    assert "--yolo" in argv


def test_hermes_cli_can_use_native_config_and_auto_context() -> None:
    client = HermesCliClient(
        config=HermesConfig(path="/bin/hermes"),
        config_scope="native",
        auto_context="enabled",
    )

    argv = client.build_chat_command(prompt="{}", max_turns=1)

    assert "--ignore-user-config" not in argv
    assert "--ignore-rules" not in argv


def test_hermes_inherit_preserves_native_tool_selection() -> None:
    client = HermesCliClient(
        config=HermesConfig(path="hermes"),
        tool_permissions="inherit",
        config_scope="native",
        auto_context="enabled",
    )
    argv = client.build_chat_command(prompt="{}", max_turns=1)
    assert not {"--toolsets", "--yolo", "--ignore-rules", "--ignore-user-config"} & set(
        argv
    )


def test_hermes_natural_coworker_session_accepts_plain_text() -> None:
    def runner(argv: list[str], timeout: int) -> AgentRunResult:
        return AgentRunResult(
            argv=argv, exit_code=0, stdout="已完成", stderr="session_id: s1"
        )

    client = HermesCliClient(config=HermesConfig(path="hermes"), runner=runner)
    result = client.natural_task_session("处理群消息", session_id="s1")
    assert result.ok and result.session_id == "s1"
    assert result.json_data is None
    assert "--resume" in result.argv


def test_hermes_cli_injects_explicit_skills_only_for_task_session() -> None:
    client = HermesCliClient(
        config=HermesConfig(path="/bin/hermes"),
        session_skills=["/skills/triage", "/skills/support"],
    )

    router_argv = client.build_chat_command(prompt="{}", max_turns=1)
    session_argv = client.build_chat_command(
        prompt="{}", max_turns=1, include_session_skills=True
    )

    assert "--skills" not in router_argv
    assert session_argv.count("--skills") == 2
    assert session_argv[session_argv.index("--skills") + 1] == "/skills/triage"
    assert (
        session_argv[
            session_argv.index("--skills", session_argv.index("--skills") + 1) + 1
        ]
        == "/skills/support"
    )


def test_hermes_structured_output_does_not_inject_task_session_skills() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> AgentRunResult:
        seen.append(argv)
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout='{"route":"ignore","target_task_id":null,"reason":"done"}',
        )

    client = HermesCliClient(
        config=HermesConfig(path="hermes"),
        session_skills=["/skills/docmate"],
        runner=runner,
    )

    result = client.structured_output("judge", output_model=TaskRouterOutput)

    assert result.ok
    assert "--skills" not in seen[0]


def test_hermes_structured_task_session_injects_task_session_skills() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> AgentRunResult:
        seen.append(argv)
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout='{"answerability":"no_reply","decision_reason":"no_response_needed",'
            '"proposed_reply":"","reply_target_message_id":null,"watch_action":"keep_watching"}',
        )

    client = HermesCliClient(
        config=HermesConfig(path="hermes"),
        session_skills=["/skills/docmate"],
        runner=runner,
    )

    result = client.structured_task_session(
        "revision task", output_model=TaskRouterOutput
    )

    assert result.ok
    assert "--skills" in seen[0]
    assert seen[0][seen[0].index("--skills") + 1] == "/skills/docmate"


def test_hermes_cli_parses_json_and_session_id() -> None:
    def runner(argv: list[str], timeout: int) -> AgentRunResult:
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout='{"route":"ignore","target_task_id":null,"reason":"not relevant"}',
            stderr="\nsession_id: 20260622_abc\n",
        )

    client = HermesCliClient(config=HermesConfig(path="hermes"), runner=runner)

    result = client.task_router("prompt")

    assert result.ok
    assert result.session_id == "20260622_abc"
    assert isinstance(result.json_data, dict)
    assert result.json_data["route"] == "ignore"


def test_hermes_cli_rejects_non_json_stdout() -> None:
    def runner(argv: list[str], timeout: int) -> AgentRunResult:
        return AgentRunResult(
            argv=argv, exit_code=0, stdout="not json", stderr="session_id: sid"
        )

    client = HermesCliClient(config=HermesConfig(path="hermes"), runner=runner)

    result = client.task_session("prompt")

    assert not result.ok
    assert result.session_id == "sid"
    assert "not valid JSON" in (result.error or "")


def test_hermes_accepts_only_the_known_tirith_warning_before_json() -> None:
    def runner(argv: list[str], timeout: int) -> AgentRunResult:
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout=(
                "\x1b[2m⚠ tirith security scanner enabled but not available "
                "— command scanning will use pattern matching only\x1b[0m\n"
                '{"route":"ignore","target_task_id":null,"reason":"done"}'
            ),
        )

    result = HermesCliClient(
        config=HermesConfig(path="hermes"), runner=runner
    ).task_session("prompt")
    assert result.ok
    assert result.json_data is not None and result.json_data["route"] == "ignore"

    def unknown_warning(argv: list[str], timeout: int) -> AgentRunResult:
        return AgentRunResult(
            argv=argv, exit_code=0, stdout='unknown warning\n{"route":"ignore"}'
        )

    result = HermesCliClient(
        config=HermesConfig(path="hermes"), runner=unknown_warning
    ).task_session("prompt")
    assert not result.ok


def test_reply_postprocess_uses_safe_toolset_without_resume_or_skills() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> AgentRunResult:
        seen.append(argv)
        return AgentRunResult(
            argv=argv, exit_code=0, stdout='{"status":"ok","final_reply":"done"}'
        )

    client = HermesCliClient(
        config=HermesConfig(
            path="/bin/hermes",
            session_max_turns=8,
            model="main-model",
            provider="main-provider",
        ),
        tool_permissions="full_access",
        reply_postprocess=ReplyPostprocessConfig(max_turns=5),
        session_skills=["/skills/task"],
        runner=runner,
    )

    result = client.reply_postprocess("prompt")

    argv = seen[0]
    assert result.ok
    assert argv[argv.index("--toolsets") + 1] == "safe"
    assert "--yolo" not in argv
    assert "--resume" not in argv
    assert "--skills" not in argv
    assert argv[argv.index("--max-turns") + 1] == "5"
    assert argv[argv.index("--model") + 1] == "main-model"
    assert argv[argv.index("--provider") + 1] == "main-provider"


def test_owner_style_refresh_uses_safe_toolset_and_postprocess_overrides() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> AgentRunResult:
        seen.append(argv)
        return AgentRunResult(
            argv=argv,
            exit_code=0,
            stdout='{"status":"ok","profile_markdown":"# Profile"}',
        )

    client = HermesCliClient(
        config=HermesConfig(
            path="/bin/hermes", model="main-model", provider="main-provider"
        ),
        tool_permissions="full_access",
        reply_postprocess=ReplyPostprocessConfig(
            max_turns=6, model="style-model", provider="style-provider"
        ),
        session_skills=["/skills/task"],
        runner=runner,
    )

    result = client.owner_style_refresh("prompt")

    argv = seen[0]
    assert result.ok
    assert argv[argv.index("--toolsets") + 1] == "safe"
    assert "--resume" not in argv
    assert "--skills" not in argv
    assert argv[argv.index("--max-turns") + 1] == "6"
    assert argv[argv.index("--model") + 1] == "style-model"
    assert argv[argv.index("--provider") + 1] == "style-provider"
