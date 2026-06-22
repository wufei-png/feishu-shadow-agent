from __future__ import annotations

from feishu_shadow_agent.config import HermesConfig
from feishu_shadow_agent.hermes import HermesCliClient
from feishu_shadow_agent.types import HermesCliResult


def test_hermes_cli_builds_quiet_safe_chat_command_with_model_provider_and_resume() -> None:
    config = HermesConfig(
        path="/bin/hermes",
        source="feishu-shadow-agent",
        toolsets="safe",
        session_max_turns=7,
        model="m",
        provider="p",
    )
    client = HermesCliClient(config=config)

    argv = client.build_chat_command(prompt='{"ok": true}', max_turns=7, session_id="sid_1")

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
        "--ignore-rules",
        "--max-turns",
        "7",
        "--resume",
        "sid_1",
        "--model",
        "m",
        "--provider",
        "p",
    ]


def test_hermes_cli_parses_json_and_session_id() -> None:
    def runner(argv: list[str], timeout: int) -> HermesCliResult:
        return HermesCliResult(
            argv=argv,
            exit_code=0,
            stdout='{"route":"ignore","confidence":1,"updated_watch_keys":[]}',
            stderr="\nsession_id: 20260622_abc\n",
        )

    client = HermesCliClient(config=HermesConfig(path="hermes"), runner=runner)

    result = client.task_router("prompt")

    assert result.ok
    assert result.session_id == "20260622_abc"
    assert result.json_data["route"] == "ignore"


def test_hermes_cli_rejects_non_json_stdout() -> None:
    def runner(argv: list[str], timeout: int) -> HermesCliResult:
        return HermesCliResult(argv=argv, exit_code=0, stdout="not json", stderr="session_id: sid")

    client = HermesCliClient(config=HermesConfig(path="hermes"), runner=runner)

    result = client.task_session("prompt")

    assert not result.ok
    assert result.session_id == "sid"
    assert "not valid JSON" in (result.error or "")
