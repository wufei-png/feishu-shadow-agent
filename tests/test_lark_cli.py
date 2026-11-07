from __future__ import annotations

import pytest

from feishu_shadow_agent.feishu.lark_cli import LarkCliClient, LarkCliCommandError
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


def test_p2p_chat_list_requires_complete_pagination_metadata() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        seen.append(argv)
        return LarkCliResult(
            argv, 0, json_data={"data": {"chats": [], "has_more": True}}
        )

    client = LarkCliClient(runner=runner)
    with pytest.raises(ValueError, match="incomplete"):
        client.list_user_p2p_chats()
    assert seen[0][seen[0].index("--types") + 1] == "p2p"
    assert seen[0][seen[0].index("--as") + 1] == "user"


def test_build_chat_messages_list_uses_order_flag() -> None:
    client = LarkCliClient(path="lark-cli")

    argv = client.build_chat_messages_list(
        as_identity="user", chat_id="oc_1", order="asc"
    )

    assert "--order" in argv
    assert "asc" in argv
    assert "--no-reactions" in argv


def test_list_chat_bots_uses_installed_read_only_command_shape() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        seen.append(argv)
        return LarkCliResult(
            argv=argv,
            exit_code=0,
            json_data={"data": {"items": [{"bot_id": "ou_bot"}]}},
        )

    result = LarkCliClient(path="lark-cli", runner=runner).list_chat_bots(
        chat_id="oc_1"
    )

    assert result.ok
    assert seen == [
        [
            "lark-cli",
            "im",
            "chat.members",
            "bots",
            "--as",
            "user",
            "--json",
            "--chat-id",
            "oc_1",
        ]
    ]


def test_run_json_preserves_structured_error_envelope_from_failed_command() -> None:
    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(
            argv=argv,
            exit_code=1,
            stdout='{"code":10002,"msg":"bot is not in the chat"}',
            error="command failed",
        )

    result = LarkCliClient(path="lark-cli", runner=runner).run_json(["lark-cli", "x"])

    assert not result.ok
    assert result.error == "command failed"
    assert result.json_data == {"code": 10002, "msg": "bot is not in the chat"}


def test_list_p2p_messages_uses_user_identity_and_user_id() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        seen.append(argv)
        return LarkCliResult(argv=argv, exit_code=0, json_data={"data": {"items": []}})

    client = LarkCliClient(path="lark-cli", runner=runner)

    page = client.list_p2p_messages(
        user_id="ou_bot",
        start="2026-06-22T00:00:00+08:00",
        end="2026-06-22T01:00:00+08:00",
    )

    assert page.items == []
    assert seen[0][:5] == ["lark-cli", "im", "+chat-messages-list", "--as", "user"]
    assert "--user-id" in seen[0]
    assert "ou_bot" in seen[0]


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


@pytest.mark.parametrize("identity", ["user", "bot"])
def test_reply_thread_flag_is_explicit_for_both_identities(identity: str) -> None:
    client = LarkCliClient()
    assert "--reply-in-thread" not in client.build_messages_reply(
        as_identity=identity, message_id="om_root", text="hello", idempotency_key="idem"
    )
    assert "--reply-in-thread" in client.build_messages_reply(
        as_identity=identity,
        message_id="om_root",
        text="hello",
        idempotency_key="idem",
        reply_in_thread=True,
    )


def test_build_messages_send_card_uses_interactive_content() -> None:
    client = LarkCliClient(path="lark-cli")

    argv = client.build_messages_send_card(
        as_identity="bot",
        user_id="ou_owner",
        card={"schema": "2.0", "body": {"elements": []}},
        idempotency_key="idem_card",
    )

    assert argv[:5] == ["lark-cli", "im", "+messages-send", "--as", "bot"]
    assert argv[argv.index("--msg-type") + 1] == "interactive"
    assert '"schema":"2.0"' in argv[argv.index("--content") + 1]
    assert "--dry-run" in argv


def test_build_messages_mget_uses_comma_separated_ids_and_limit() -> None:
    client = LarkCliClient(path="lark-cli")

    argv = client.build_messages_mget(as_identity="user", message_ids=["om_1", "om_2"])

    assert argv == [
        "lark-cli",
        "im",
        "+messages-mget",
        "--as",
        "user",
        "--json",
        "--message-ids",
        "om_1,om_2",
        "--no-reactions",
    ]
    with pytest.raises(ValueError, match="at most 50"):
        client.build_messages_mget(
            as_identity="user", message_ids=[f"om_{i}" for i in range(51)]
        )


def test_run_json_strips_lark_cli_dry_run_banner() -> None:
    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(
            argv=argv,
            exit_code=0,
            stdout='=== Dry Run ===\n{"api": [{"method": "POST"}]}',
        )

    client = LarkCliClient(path="lark-cli", runner=runner)

    result = client.run_json(
        ["lark-cli", "im", "+messages-reply", "--dry-run", "--json"]
    )

    assert result.ok
    assert result.json_data == {"api": [{"method": "POST"}]}


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


def test_run_json_accepts_only_the_known_warning_before_complete_json() -> None:
    warning = "warning: reactions_partial_failed: 1 message(s) failed (om_1)"
    stdout = f'{warning}\n{{"ok":true,"data":{{"messages":[]}}}}'

    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(argv=argv, exit_code=0, stdout=stdout)

    client = LarkCliClient(path="lark-cli", runner=runner)
    page = client.get_messages(as_identity="user", message_ids=["om_1"])
    assert page.items == []
    assert page.warnings == (warning,)
    result = client.run_json(["lark-cli", "x"])
    assert result.stdout == stdout

    for invalid in (
        'warning: something_else\n{"ok":true}',
        'warning: reactions_partial_failed_extra: unexpected\n{"ok":true}',
        'warning: reactions_partial_failed\n{"ok":true}\nextra',
        'warning: reactions_partial_failed\n{"ok":true}{"ok":true}',
        "",
    ):
        stdout = invalid
        assert not client.run_json(["lark-cli", "x"]).ok


def test_message_page_preserves_known_warning_from_stderr() -> None:
    warning = "warning: reactions_partial_failed: 2 message(s) failed (om_1, om_2)"

    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(
            argv=argv,
            exit_code=0,
            stdout='{"ok":true,"data":{"messages":[]}}',
            stderr=f"unrelated diagnostic\n{warning}\n",
        )

    page = LarkCliClient(runner=runner).get_messages(
        as_identity="user", message_ids=["om_1"]
    )
    assert page.warnings == (warning,)


def test_run_json_does_not_treat_error_envelope_as_success() -> None:
    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(
            argv=argv, exit_code=0, stdout='{"ok":false,"error":"failed"}'
        )

    result = LarkCliClient(runner=runner).run_json(["lark-cli", "x"])
    assert not result.ok
    assert result.json_data == {"ok": False, "error": "failed"}


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"ok": False, "data": {"messages": []}},
        {"code": 10002, "data": {"messages": []}},
        {"data": {}},
        {"data": {"messages": None}},
        {"data": {"messages": [{"message_id": "om_1"}, None]}},
        {"data": {"messages": [], "has_more": True}},
        {"data": {"messages": [], "has_more": "unknown"}},
        {"data": [], "has_more": True},
        {"data": [], "has_more": "unknown"},
        {"data": {"messages": [], "page_token": "inner"}, "page_token": "outer"},
    ],
)
def test_invalid_message_page_is_not_an_empty_success(data: object) -> None:
    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(argv=argv, exit_code=0, json_data=data)

    with pytest.raises(LarkCliCommandError):
        LarkCliClient(runner=runner).get_messages(
            as_identity="user", message_ids=["om_1"]
        )


@pytest.mark.parametrize(
    "data",
    [
        {"data": [{"message_id": "om_1"}], "has_more": True, "page_token": "next"},
        {
            "data": {"messages": [{"message_id": "om_1"}]},
            "has_more": True,
            "page_token": "next",
        },
    ],
)
def test_message_page_preserves_pagination_from_outer_envelope(data: object) -> None:
    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        return LarkCliResult(argv=argv, exit_code=0, json_data=data)

    page = LarkCliClient(runner=runner).get_messages(
        as_identity="user", message_ids=["om_1"]
    )
    assert page.items == [{"message_id": "om_1"}]
    assert page.has_more and page.next_page_token == "next"


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
            json_data={
                "data": {"items": [{"message_id": "om_1"}], "page_token": "next"}
            },
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


def test_search_messages_batches_within_requested_page_limit() -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        seen.append(argv)
        return LarkCliResult(
            argv=argv,
            exit_code=0,
            json_data={
                "data": {
                    "messages": [{"message_id": f"om_{index}"} for index in range(100)],
                    "page_token": "next",
                    "has_more": True,
                }
            },
        )

    page = LarkCliClient(path="lark-cli", runner=runner).search_messages(
        chat_type="group",
        is_at_me=True,
        start="2026-06-22T00:00:00+08:00",
        end="2026-06-22T01:00:00+08:00",
        page_size=50,
        page_limit=2,
    )

    assert len(page.items) == 100
    assert page.page_count == 2
    assert page.next_page_token == "next"
    assert "--page-all" in seen[0]
    assert seen[0][seen[0].index("--page-limit") + 1] == "2"


def test_search_owner_messages_uses_sender_time_filters_page_all_and_no_reactions() -> (
    None
):
    seen: list[list[str]] = []

    def runner(argv: list[str], timeout: int) -> LarkCliResult:
        seen.append(argv)
        return LarkCliResult(
            argv=argv,
            exit_code=0,
            json_data={"data": {"items": [{"message_id": "om_1"}]}},
        )

    client = LarkCliClient(path="lark-cli", runner=runner)

    page = client.search_owner_messages(
        sender="ou_owner",
        start="2026-06-01T00:00:00+08:00",
        end="2026-07-01T00:00:00+08:00",
    )

    assert page.items == [{"message_id": "om_1"}]
    argv = seen[0]
    assert argv[:5] == ["lark-cli", "im", "+messages-search", "--as", "user"]
    assert "--sender" in argv
    assert "ou_owner" in argv
    assert "--start" in argv
    assert "--end" in argv
    assert "--page-all" in argv
    assert "--no-reactions" in argv
    client.search_owner_messages(
        sender="ou_owner",
        start=None,
        end=None,
        chat_type="group",
        chat_id="oc_1",
        page_token="next",
        page_all=False,
    )
    assert "--page-all" not in seen[1]
    assert seen[1][seen[1].index("--page-token") + 1] == "next"


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
