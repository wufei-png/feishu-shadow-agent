from __future__ import annotations

import argparse
import html
import importlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+")


class PartnerConfigError(ValueError):
    pass


class HermesGatewayPartnerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open_id: str = Field(
        pattern=r"^ou_[A-Za-z0-9_]+$",
        description="Partner bot open ID in the Gateway application's identity space.",
    )
    description: str = Field(
        default="",
        max_length=300,
        description="Brief responsibilities of this partner.",
    )

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return " ".join(value.split())


class HermesGatewayGroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partners: dict[str, HermesGatewayPartnerConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_partners(self) -> HermesGatewayGroupConfig:
        if any(
            not name.strip()
            or name != name.strip()
            or len(name) > 80
            or any(ord(char) < 32 for char in name)
            for name in self.partners
        ):
            raise ValueError("partner names must be non-empty single-line names")
        identifiers = [partner.open_id for partner in self.partners.values()]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("partner open IDs must be unique within a group")
        return self


class HermesGatewayCoworkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: dict[str, HermesGatewayGroupConfig] = Field(
        default_factory=dict,
        description="Gateway partner rosters keyed by Feishu group chat ID.",
    )

    @field_validator("groups")
    @classmethod
    def validate_groups(
        cls, value: dict[str, HermesGatewayGroupConfig]
    ) -> dict[str, HermesGatewayGroupConfig]:
        if any(re.fullmatch(r"oc_[A-Za-z0-9_]+", chat_id) is None for chat_id in value):
            raise ValueError("Gateway partner groups must be Feishu oc_ chat IDs")
        return value


def _load_yaml(path: Path) -> Any:
    try:
        ruamel = cast(Any, importlib.import_module("ruamel.yaml"))
    except ModuleNotFoundError as exc:
        if exc.name not in {"ruamel", "ruamel.yaml"}:
            raise
        import yaml

        from feishu_shadow_agent.config import _UniqueKeySafeLoader  # pyright: ignore[reportPrivateUsage]

        return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)  # noqa: S506
    return ruamel.YAML(typ="safe").load(path.read_text(encoding="utf-8"))


class GatewayPartners:
    def __init__(self, config_path: str | Path) -> None:
        path = Path(config_path).expanduser()
        if not path.is_absolute():
            raise ValueError("the Gateway partner config path must be absolute")
        self.config_path = path.resolve()
        try:
            raw = _load_yaml(self.config_path)
            if not isinstance(raw, dict):
                raise ValueError("config root must be a mapping")
            self.groups = HermesGatewayCoworkerConfig.model_validate(
                cast(dict[str, Any], raw).get("hermes_gateway_coworker", {})
            ).groups
        except Exception as exc:
            raise PartnerConfigError(f"invalid Gateway partner config: {exc}") from exc

    def group(self, chat_id: str) -> HermesGatewayGroupConfig | None:
        return self.groups.get(chat_id)

    def name_for(self, chat_id: str, open_id: str) -> str | None:
        group = self.group(chat_id)
        if group is None:
            return None
        return next(
            (
                name
                for name, partner in group.partners.items()
                if partner.open_id == open_id
            ),
            None,
        )

    def accepts(self, chat_id: str, open_id: str) -> bool:
        return self.name_for(chat_id, open_id) is not None

    def append_prompt(
        self,
        original: str | None,
        *,
        chat_id: str,
        is_group: bool,
        profile: str,
        python: str,
    ) -> str | None:
        group = self.group(chat_id) if is_group else None
        if group is None or not group.partners:
            return original
        partners = "；".join(
            f"{json.dumps(name, ensure_ascii=False)}：{partner.description}"
            if partner.description
            else json.dumps(name, ensure_ascii=False)
            for name, partner in group.partners.items()
        )
        command = shlex.join(
            [
                python,
                str(Path(__file__).resolve()),
                "--config",
                str(self.config_path),
                "--profile",
                profile,
            ]
        )
        addition = f"群聊伙伴：{partners}。\n委派：{command} '伙伴名称' '任务内容'"
        return f"{original}\n\n{addition}" if original else addition


def send_to_partner(
    roster: GatewayPartners,
    partner_name: str,
    task: str,
    *,
    profile: str,
    environ: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    chat_id = environ.get("HERMES_SESSION_CHAT_ID", "")
    thread_id = environ.get("HERMES_SESSION_THREAD_ID", "")
    chat_type = environ.get("HERMES_SESSION_CHAT_TYPE", "").lower()
    if (
        environ.get("HERMES_SESSION_PLATFORM", "").lower() != "feishu"
        or chat_type not in {"group", "channel", "thread", "forum"}
        or not re.fullmatch(r"oc_[A-Za-z0-9_]+", chat_id)
        or (thread_id and not _IDENTIFIER.fullmatch(thread_id))
        or not _IDENTIFIER.fullmatch(profile)
    ):
        raise ValueError(
            "a valid Feishu group session and transport profile are required"
        )
    group = roster.group(chat_id)
    if group is None or partner_name not in group.partners:
        raise ValueError("this partner is not configured for the current group")
    if not task.strip():
        raise ValueError("the task must not be empty")
    partner = group.partners[partner_name]
    mention = f'<at user_id="{partner.open_id}">{html.escape(partner_name)}</at>'
    target = f"feishu:{chat_id}" + (f":{thread_id}" if thread_id else "")
    executable = shutil.which("hermes")
    if executable is None:
        raise ValueError("the Hermes executable is not available")
    return subprocess.run(  # noqa: S603
        [executable, "-p", profile, "send", "--json", "--to", target],
        input=f"{mention} {task.strip()}",
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delegate to a configured Feishu group partner"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("partner")
    parser.add_argument("task")
    args = parser.parse_args(argv)
    try:
        result = send_to_partner(
            GatewayPartners(args.config),
            args.partner,
            args.task,
            profile=args.profile,
            environ=os.environ,
        )
    except (OSError, ValueError) as exc:
        print(f"委派失败：{exc}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("发送超时，结果未知；请先核对群消息再决定是否重发。", file=sys.stderr)
        return 1
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode:
        return result.returncode
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        print("发送结果无法解析，请先核对群消息再决定是否重发。", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("发送结果无法解析，请先核对群消息再决定是否重发。", file=sys.stderr)
        return 1
    result_data = cast(dict[str, Any], payload)
    if result_data.get("success") is not True or result_data.get("skipped"):
        print("消息未确认发送，请检查上述结果。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
