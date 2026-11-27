from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from feishu_shadow_agent.config import ConfigError, ConfigService
from feishu_shadow_agent.hermes_gateway_partners import (
    GatewayPartners,
    PartnerConfigError,
    main,
    send_to_partner,
)


@pytest.fixture
def partner_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        """owner:
  open_id: ou_owner
hermes_gateway_coworker:
  groups:
    oc_one:
      partners:
        构建伙伴:
          open_id: ou_build
          description: 检查构建和测试
    oc_two:
      partners:
        设计伙伴:
          open_id: ou_design
          description: 审查设计
    oc_empty:
      partners: {}
""",
        encoding="utf-8",
    )
    return path


def test_prompt_is_appended_only_for_the_configured_group(partner_config: Path) -> None:
    partners = GatewayPartners(partner_config)
    prompt = partners.append_prompt(
        "已有群提示",
        chat_id="oc_one",
        is_group=True,
        profile="coworker",
        python="/x/python",
    )

    assert prompt is not None
    assert prompt.startswith("已有群提示\n\n群聊伙伴：")
    assert "构建伙伴" in prompt and "检查构建和测试" in prompt
    assert "设计伙伴" not in prompt
    assert "委派：/x/python " in prompt

    for chat_id, is_group in (
        ("oc_unknown", True),
        ("oc_empty", True),
        ("oc_one", False),
    ):
        for original in (None, "", "已有群提示"):
            assert (
                partners.append_prompt(
                    original,
                    chat_id=chat_id,
                    is_group=is_group,
                    profile="coworker",
                    python="/x/python",
                )
                == original
            )


def test_partner_identity_is_scoped_to_the_current_group(partner_config: Path) -> None:
    partners = GatewayPartners(partner_config)
    assert partners.accepts("oc_one", "ou_build")
    assert not partners.accepts("oc_one", "ou_design")
    assert not partners.accepts("oc_two", "ou_build")
    assert not partners.accepts("oc_unknown", "ou_build")
    assert partners.name_for("oc_one", "ou_build") == "构建伙伴"
    assert partners.name_for("oc_two", "ou_design") == "设计伙伴"
    assert partners.name_for("oc_one", "ou_design") is None
    assert partners.name_for("oc_two", "ou_build") is None
    assert partners.name_for("oc_unknown", "ou_build") is None


def test_send_uses_group_topic_and_hermes_transport(partner_config: Path) -> None:
    partners = GatewayPartners(partner_config)
    env = {
        "HERMES_SESSION_PLATFORM": "feishu",
        "HERMES_SESSION_CHAT_TYPE": "group",
        "HERMES_SESSION_CHAT_ID": "oc_one",
        "HERMES_SESSION_THREAD_ID": "omt_topic",
    }
    result = subprocess.CompletedProcess([], 0, '{"success": true}', "")
    with (
        patch(
            "feishu_shadow_agent.hermes_gateway_partners.shutil.which",
            return_value="/bin/hermes",
        ),
        patch(
            "feishu_shadow_agent.hermes_gateway_partners.subprocess.run",
            return_value=result,
        ) as run,
    ):
        assert (
            send_to_partner(
                partners, "构建伙伴", "检查 $(danger)", profile="coworker", environ=env
            )
            is result
        )

    args, kwargs = run.call_args
    assert args[0] == [
        "/bin/hermes",
        "-p",
        "coworker",
        "send",
        "--json",
        "--to",
        "feishu:oc_one:omt_topic",
    ]
    assert kwargs["input"] == '<at user_id="ou_build">构建伙伴</at> 检查 $(danger)'
    assert kwargs["timeout"] == 90


def test_command_runs_end_to_end_with_a_fake_hermes(
    partner_config: Path, tmp_path: Path
) -> None:
    executable = tmp_path / "hermes"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'success': True, 'args': sys.argv[1:], 'body': sys.stdin.read()}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "HERMES_SESSION_PLATFORM": "feishu",
        "HERMES_SESSION_CHAT_TYPE": "forum",
        "HERMES_SESSION_CHAT_ID": "oc_one",
        "HERMES_SESSION_THREAD_ID": "omt_topic",
    }
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "feishu_shadow_agent.hermes_gateway_partners",
            "--config",
            str(partner_config),
            "--profile",
            "coworker",
            "构建伙伴",
            "检查构建",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    sent = json.loads(result.stdout)
    assert sent["args"] == [
        "-p",
        "coworker",
        "send",
        "--json",
        "--to",
        "feishu:oc_one:omt_topic",
    ]
    assert sent["body"] == '<at user_id="ou_build">构建伙伴</at> 检查构建'


@pytest.mark.parametrize(
    "overrides",
    [
        {"HERMES_SESSION_PLATFORM": "telegram"},
        {"HERMES_SESSION_CHAT_TYPE": "dm"},
        {"HERMES_SESSION_CHAT_ID": "oc_two"},
        {"HERMES_SESSION_THREAD_ID": "../../elsewhere"},
    ],
)
def test_send_rejects_wrong_context(
    partner_config: Path, overrides: dict[str, str]
) -> None:
    env = {
        "HERMES_SESSION_PLATFORM": "feishu",
        "HERMES_SESSION_CHAT_TYPE": "group",
        "HERMES_SESSION_CHAT_ID": "oc_one",
        **overrides,
    }
    with (
        pytest.raises(ValueError),
        patch("feishu_shadow_agent.hermes_gateway_partners.subprocess.run") as run,
    ):
        send_to_partner(
            GatewayPartners(partner_config),
            "构建伙伴",
            "检查",
            profile="coworker",
            environ=env,
        )
    run.assert_not_called()


def test_rejects_duplicate_partner_identity_and_yaml_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "owner: {open_id: ou_owner}\n"
        "hermes_gateway_coworker:\n  groups:\n    oc_one:\n      partners:\n"
        "        A: {open_id: ou_same}\n        B: {open_id: ou_same}\n",
        encoding="utf-8",
    )
    with pytest.raises(PartnerConfigError):
        GatewayPartners(path)
    with pytest.raises(ConfigError):
        ConfigService().load(path)

    path.write_text(
        "owner: {open_id: ou_owner}\nhermes_gateway_coworker:\n  groups:\n"
        "    oc_one: {}\n    oc_one: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(PartnerConfigError):
        GatewayPartners(path)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"success": true, "message_id": "om_one"}', 0),
        ('{"success": true, "skipped": true}', 1),
        ('{"success": false, "error": "delivery uncertain"}', 1),
        ("unexpected output", 1),
    ],
)
def test_command_reports_the_actual_send_result(
    partner_config: Path, stdout: str, expected: int
) -> None:
    result = subprocess.CompletedProcess([], 0, stdout, "")
    with patch(
        "feishu_shadow_agent.hermes_gateway_partners.send_to_partner",
        return_value=result,
    ):
        assert (
            main(
                [
                    "--config",
                    str(partner_config),
                    "--profile",
                    "coworker",
                    "构建伙伴",
                    "检查",
                ]
            )
            == expected
        )
