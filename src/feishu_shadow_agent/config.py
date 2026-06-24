from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator, model_validator

from .agent_backend import AgentBackendProvider

CONFIG_ENV_VAR = "FEISHU_SHADOW_AGENT_CONFIG"


class ConfigError(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OwnerConfig(StrictModel):
    open_id: str = Field(
        min_length=1,
        description="Feishu open_id of the single owner who receives approval notifications and can run local commands.",
    )
    name: str = Field(default="", description="Optional display name for humans reading config and logs.")


class DaemonConfig(StrictModel):
    tick_interval_seconds: int = Field(default=60, gt=0, description="Seconds between daemon polling ticks.")
    overlap_seconds: int = Field(
        default=120,
        ge=0,
        description="Look-back window added to message fetches so delayed Feishu results are not missed.",
    )


class HealthConfig(StrictModel):
    interval_seconds: int = Field(default=300, gt=0, description="Seconds between full runtime health refreshes.")
    retry_interval_seconds: int = Field(
        default=60,
        gt=0,
        description="Seconds to wait before retrying runtime health after a critical failure.",
    )
    timeout_seconds: int = Field(default=10, gt=0, description="Default timeout in seconds for health probes.")


class StorageConfig(StrictModel):
    sqlite_path: str = Field(
        default="data/agent.sqlite3",
        description="SQLite database path, resolved relative to the config file when not absolute.",
    )
    resource_dir: str = Field(
        default="data/resources",
        description="Safe relative directory for downloaded message resources; absolute paths and '..' are rejected.",
    )

    @field_validator("resource_dir")
    @classmethod
    def validate_resource_dir(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("storage.resource_dir must be a safe relative path")
        return value


class LoggingConfig(StrictModel):
    jsonl_path: str = Field(
        default="logs/agent.jsonl",
        description="JSONL log path, resolved relative to the config file when not absolute.",
    )
    level: Literal["debug", "info", "warning", "error"] = Field(
        default="info",
        description="Minimum runtime log level written to configured sinks.",
    )
    console: StrictBool = Field(
        default=False,
        description="Whether to also write human-readable runtime logs to stderr.",
    )
    text_path: str | None = Field(
        default=None,
        description="Optional human-readable log file path, resolved relative to the config file when not absolute.",
    )


class LarkCliConfig(StrictModel):
    path: str | None = Field(
        default=None,
        description="Optional lark-cli executable path; null uses the current PATH.",
    )
    timeout_seconds: int = Field(default=30, gt=0, description="Timeout in seconds for lark-cli subprocess calls.")


class HermesConfig(StrictModel):
    mode: Literal["cli", "http"] = Field(
        default="cli",
        description="Hermes integration mode. 'cli' uses the local Hermes executable; 'http' keeps compatibility with a health URL.",
    )
    path: str | None = Field(
        default=None,
        description="Optional Hermes executable path for cli mode; null uses the current PATH.",
    )
    source: str = Field(default="feishu-shadow-agent", description="Source label passed to Hermes sessions and audit data.")
    router_max_turns: int = Field(default=4, gt=0, description="Maximum Hermes turns for task routing calls.")
    session_max_turns: int = Field(default=8, gt=0, description="Maximum Hermes turns for task session calls.")
    model: str | None = Field(default=None, description="Optional Hermes model override; null uses the Hermes default.")
    provider: str | None = Field(default=None, description="Optional Hermes provider override; null uses the Hermes default.")
    timeout_seconds: int = Field(default=60, gt=0, description="Timeout in seconds for Hermes subprocess or health calls.")
    health_url: str | None = Field(
        default=None,
        description="HTTP health URL used only when hermes.mode is 'http'; must start with http:// or https://.",
    )
    api_key_env: str | None = Field(
        default="HERMES_API_KEY",
        description="Environment variable name that holds the Hermes API key for http mode; the secret value is never stored here.",
    )

    @field_validator("health_url")
    @classmethod
    def validate_health_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError("agent_backend.hermes.health_url must start with http:// or https://")
        return value

    @field_validator("source")
    @classmethod
    def validate_non_empty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value

    @model_validator(mode="after")
    def validate_http_mode(self) -> "HermesConfig":
        if self.mode == "http" and not self.health_url:
            raise ValueError("agent_backend.hermes.health_url is required when agent_backend.hermes.mode is http")
        return self


RiskLevel = Literal["low", "medium", "high"]
ToolPermissionsProfile = Literal["read_only", "guarded_write", "full_access"]
ConfigScopeMode = Literal["isolated", "native"]
AutoContextMode = Literal["disabled", "enabled"]


class ExplicitAgentContextConfig(StrictModel):
    skills: list[str] = Field(
        default_factory=list,
        description="Explicit skill directory paths or SKILL.md file paths to pass only to task-session agent turns.",
    )

    @field_validator("skills")
    @classmethod
    def validate_skills(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("agent_backend.explicit_context.skills entries must not be empty")
        return cleaned


class AgentBackendConfig(StrictModel):
    provider: AgentBackendProvider = Field(
        default="hermes",
        description="Agent backend provider. Only hermes is implemented in this release; codex and claude_code are reserved.",
    )
    config_scope: ConfigScopeMode = Field(
        default="isolated",
        description="Whether agent CLI calls load user-global configuration or run isolated from it.",
    )
    auto_context: AutoContextMode = Field(
        default="disabled",
        description="Whether agent CLI calls auto-load rules, memory, and implicit skill context.",
    )
    explicit_context: ExplicitAgentContextConfig = Field(
        default_factory=ExplicitAgentContextConfig,
        description="Context explicitly injected by feishu-shadow-agent instead of discovered from user-global state.",
    )
    hermes: HermesConfig = Field(default_factory=HermesConfig, description="Hermes backend settings.")

    @model_validator(mode="after")
    def validate_provider(self) -> "AgentBackendConfig":
        if self.provider != "hermes":
            raise ValueError(f"agent_backend.provider={self.provider!r} is reserved but not implemented")
        return self


class ReplyPolicyConfig(StrictModel):
    p2p_auto_reply: StrictBool = Field(
        default=True,
        description="Whether one-to-one chats may auto-reply when risk and confidence gates pass.",
    )
    default_group_auto_reply: StrictBool = Field(
        default=False,
        description="Default auto-reply behavior for groups without an explicit chats entry.",
    )
    risk_level_max: RiskLevel = Field(
        default="low",
        description="Maximum risk level allowed for automatic replies: low, medium, or high.",
    )
    confidence_threshold: float = Field(
        default=0.85,
        ge=0,
        le=1,
        description="Minimum Hermes confidence required for automatic replies.",
    )


class ChatPolicyConfig(StrictModel):
    name: str = Field(default="", description="Human-readable chat name for operators; not used as an identifier.")
    auto_reply: StrictBool = Field(default=False, description="Whether this chat may auto-reply when all gates pass.")
    bot_joined: StrictBool = Field(
        default=False,
        description="Whether the bot has joined this chat and can be used for bot replies and resource access.",
    )
    reply_identity: Literal["bot_preferred", "bot", "user"] = Field(
        default="bot_preferred",
        description="Reply identity for this chat: prefer bot with fallback, require bot, or send as user.",
    )
    allow_user_fallback: StrictBool = Field(
        default=True,
        description="Whether group replies may fall back to user identity when bot_preferred cannot use the bot.",
    )
    resource_download: StrictBool = Field(
        default=True,
        description="Whether downloadable message resources may be saved for this chat.",
    )
    risk_level_max: RiskLevel = Field(
        default="low",
        description="Chat-specific maximum risk level allowed for automatic replies.",
    )
    confidence_threshold: float = Field(
        default=0.85,
        ge=0,
        le=1,
        description="Chat-specific minimum Hermes confidence required for automatic replies.",
    )


class RetentionConfig(StrictModel):
    raw_message_days: int = Field(default=30, ge=1, description="Days to retain raw message payloads.")
    resource_days: int = Field(default=30, ge=1, description="Days to retain downloaded resource files.")


class DebugConfig(StrictModel):
    save_full_agent_io: StrictBool = Field(
        default=False,
        description="Whether to persist full agent prompts and outputs for debugging; keep false for normal operation.",
    )


class AppConfig(StrictModel):
    owner: OwnerConfig = Field(description="Single owner identity used for approvals and operator notifications.")
    daemon: DaemonConfig = Field(default_factory=DaemonConfig, description="Daemon polling and overlap settings.")
    health: HealthConfig = Field(default_factory=HealthConfig, description="Runtime health refresh and timeout settings.")
    storage: StorageConfig = Field(default_factory=StorageConfig, description="Local SQLite and resource storage settings.")
    logging: LoggingConfig = Field(default_factory=LoggingConfig, description="Local structured logging settings.")
    lark_cli: LarkCliConfig = Field(default_factory=LarkCliConfig, description="lark-cli executable and timeout settings.")
    agent_backend: AgentBackendConfig = Field(
        default_factory=AgentBackendConfig,
        description="Agent backend provider, isolation policy, explicit context, and provider-specific settings.",
    )
    reply_policy: ReplyPolicyConfig = Field(
        default_factory=ReplyPolicyConfig,
        description="Global auto-reply policy used before chat-specific overrides.",
    )
    chats: dict[str, ChatPolicyConfig] = Field(
        default_factory=dict,
        description="Per-chat policy overrides keyed by Feishu chat_id, such as oc_xxx.",
    )
    tool_permissions: ToolPermissionsProfile = Field(
        default="guarded_write",
        description="Agent backend tool permission profile: read_only, guarded_write, or full_access.",
    )
    retention: RetentionConfig = Field(default_factory=RetentionConfig, description="Local data retention settings.")
    debug: DebugConfig = Field(default_factory=DebugConfig, description="Debug-only persistence settings.")


@dataclass(frozen=True)
class LoadedConfig:
    config: AppConfig
    path: Path
    base_dir: Path


class ConfigService:
    def __init__(self, default_path: str | Path = "config.yaml", env_var: str = CONFIG_ENV_VAR):
        self.default_path = Path(default_path)
        self.env_var = env_var

    def resolve_path(self, explicit_path: str | Path | None = None) -> Path:
        if explicit_path:
            return Path(explicit_path).expanduser()
        env_path = os.environ.get(self.env_var)
        if env_path:
            return Path(env_path).expanduser()
        return self.default_path

    def load(self, explicit_path: str | Path | None = None) -> LoadedConfig:
        path = self.resolve_path(explicit_path)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"failed to read config file: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a mapping")
        try:
            config = AppConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc
        return LoadedConfig(config=config, path=path, base_dir=path.resolve().parent)

    def redacted_dict(self, config: AppConfig) -> dict[str, Any]:
        data = config.model_dump(mode="json")
        return self._redact_mapping(data)

    def json_schema_dict(self) -> dict[str, Any]:
        return AppConfig.model_json_schema()

    def _redact_mapping(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, child in value.items():
                lowered = str(key).lower()
                if lowered.endswith("_env"):
                    # *_env values are environment variable names, not secrets.
                    # Keeping them visible makes redacted config output useful.
                    redacted[key] = self._redact_mapping(child)
                elif any(marker in lowered for marker in ("secret", "token", "api_key", "password")):
                    redacted[key] = "<redacted>"
                else:
                    redacted[key] = self._redact_mapping(child)
            return redacted
        if isinstance(value, list):
            return [self._redact_mapping(item) for item in value]
        return value
