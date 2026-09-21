from __future__ import annotations

import pytest

from feishu_shadow_agent.membership import classify_bot_membership_absence
from feishu_shadow_agent.types import LarkCliResult


def _failed_result(
    argv: list[str], *, json_data: object | None = None, stderr: str = ""
) -> LarkCliResult:
    return LarkCliResult(
        argv=argv,
        exit_code=1,
        json_data=json_data,
        stderr=stderr,
        error="command failed",
    )


@pytest.mark.parametrize(
    ("json_data", "description"),
    [
        ({"code": 234002, "msg": "authentication failed"}, "authentication"),
        ({"code": 234040, "msg": "message is invisible"}, "visibility"),
        ({"code": 11221, "msg": "image does not belong to bot"}, "resource ownership"),
        ({"code": 99991679, "msg": "missing scope"}, "scope"),
        ({"code": 10002, "msg": "bot is not in chat"}, "confirmed absence"),
    ],
)
def test_classify_bot_membership_absence_requires_supported_error_code(
    json_data: object, description: str
) -> None:
    result = _failed_result(
        [
            "lark-cli",
            "im",
            "+messages-resources-download",
            "--as",
            "bot",
        ],
        json_data=json_data,
    )

    classification = classify_bot_membership_absence(result)

    if description == "confirmed absence":
        assert classification is not None
        assert classification.endpoint == "resource_download"
        assert classification.error_code == 10002
    else:
        assert classification is None


def test_classify_bot_membership_absence_rejects_plain_text_false_positive() -> None:
    result = _failed_result(
        ["lark-cli", "im", "+messages-reply", "--as", "bot"],
        stderr="error 10002: bot is not in the chat",
    )

    assert classify_bot_membership_absence(result) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["lark-cli", "im", "+messages-resources-download", "--as", "user"],
        ["lark-cli", "im", "+messages-send", "--as", "bot"],
    ],
)
def test_classify_bot_membership_absence_requires_supported_endpoint_and_identity(
    argv: list[str],
) -> None:
    result = _failed_result(argv, json_data={"code": 10002})

    assert classify_bot_membership_absence(result) is None


def test_classify_bot_membership_absence_accepts_wrapped_cli_error() -> None:
    result = _failed_result(
        ["lark-cli", "im", "+messages-reply", "--as", "bot"],
        json_data={"ok": False, "error": {"code": "10002"}},
    )

    classification = classify_bot_membership_absence(result)

    assert classification is not None
    assert classification.endpoint == "message_reply"
