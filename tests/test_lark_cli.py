from __future__ import annotations

import pytest

from feishu_shadow_agent.feishu.lark_cli import LarkCliClient
from feishu_shadow_agent.types import LarkCliResult


def test_build_messages_search_for_group_at_me() -> None:
    client = LarkCliClient(path="/bin/lark-cli")

    argv = client.build_messages_search(
        chat_type="group",
        is_at_me=True,
        start="2026-06-22T00:00:00+08:00",
        end="2026-06-22T01:00:00+08:00",
    )

    assert argv == [
        "/bin/lark-cli",
        "im",
        "+messages-search",
        "--as",
        "user",
        "--json",
        "--chat-type",
        "group",
        "--start",
        "2026-06-22T00:00:00+08:00",
        "--end",
        "2026-06-22T01:00:00+08:00",
        "--is-at-me",
        "--no-reactions",
    ]


def test_build_chat_messages_list_uses_order_flag() -> None:
    client = LarkCliClient(path="lark-cli")

    argv = client.build_chat_messages_list(as_identity="user", chat_id="oc_1", order="asc")

    assert "--order" in argv
    assert "asc" in argv
    assert "--no-reactions" in argv


def test_build_messages_send_defaults_to_dry_run_and_can_send_test() -> None:
    client = LarkCliClient(path="lark-cli")

    dry_run_argv = client.build_messages_send(
        as_identity="bot",
        user_id="ou_owner",
        text="hello",
        idempotency_key="idem_1",
    )
    send_argv = client.build_messages_send(
        as_identity="bot",
        user_id="ou_owner",
        text="hello",
        idempotency_key="idem_1",
        dry_run=False,
    )

    assert "--dry-run" in dry_run_argv
    assert "--dry-run" not in send_argv


def test_resource_output_must_be_safe_relative_path() -> None:
    client = LarkCliClient(path="lark-cli")

    with pytest.raises(ValueError, match="safe relative"):
        client.build_resources_download(
            as_identity="bot",
            message_id="om_1",
            file_key="img_1",
            resource_type="image",
            output="../secret.png",
        )


def test_run_json_returns_structured_error_for_non_json_stdout() -> None:
    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(argv=argv, exit_code=0, stdout="not-json")

    client = LarkCliClient(path="lark-cli", runner=runner)

    result = client.run_json(["lark-cli", "x", "--json"])

    assert not result.ok
    assert "not valid JSON" in (result.error or "")


def test_run_json_preserves_command_failure() -> None:
    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(argv=argv, exit_code=3, stderr="bad", error="bad")

    client = LarkCliClient(path="lark-cli", runner=runner)

    result = client.run_json(["lark-cli", "x", "--json"])

    assert result.exit_code == 3
    assert result.error == "bad"


def test_search_messages_returns_message_page() -> None:
    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(
            argv=argv,
            exit_code=0,
            json_data={"data": {"items": [{"message_id": "om_1"}], "page_token": "next"}},
        )

    client = LarkCliClient(path="lark-cli", runner=runner)

    page = client.search_messages(
        chat_type="group",
        is_at_me=True,
        start="2026-06-22T00:00:00+08:00",
        end="2026-06-22T01:00:00+08:00",
    )

    assert page.items == [{"message_id": "om_1"}]
    assert page.has_more is True
    assert page.next_page_token == "next"


def test_download_resource_uses_bot_identity_and_actual_download() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        seen.append(argv)
        return LarkCliResult(argv=argv, exit_code=0, json_data={})

    client = LarkCliClient(path="lark-cli", runner=runner)

    result = client.download_resource(
        message_id="om_1",
        file_key="img_1",
        resource_type="image",
        output="data/resources/om_1/image.bin",
    )

    assert result.ok
    assert "--as" in seen[0]
    assert "bot" in seen[0]
    assert "--dry-run" not in seen[0]
