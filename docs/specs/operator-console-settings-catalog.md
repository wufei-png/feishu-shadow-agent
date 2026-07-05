# Operator Console Settings Catalog

Status: draft

This document defines the console-exposed settings field map for the local
Operator Console. It is a stable product map, not a dynamic form engine and not
a mirror of every raw `AppConfig` field.

## Purpose

The Settings Catalog exists so the console can present settings as product
choices instead of dumping raw YAML or Pydantic schema fields.

It answers:

- Which settings the console may expose.
- Which settings are editable in v1.
- Where each setting is stored.
- Which settings belong in normal, advanced, readonly, diagnostic, or hidden UI.
- Which command or API boundary owns future writes.

It does not answer:

- Exact React form layout.
- Exact local API route implementation.
- Future `config.yaml` write mechanics.
- Remote or multi-owner administration.

## Catalog Entry Shape

Each console-exposed field should have:

```text
key
label
description
help
source
scope
visibility
editable_v1
requires_restart
audit_behavior
write_boundary
```

Allowed sources:

```text
product_policy_store
config_yaml
runtime_status
derived
```

Allowed visibility values:

```text
normal
advanced
readonly
diagnostic
hidden
```

Rules:

- Do not auto-render raw Pydantic schema.
- Do not expose every config field just because it exists.
- Do not use risk levels or UI-owned risk taxonomy.
- Do not write `config.yaml` in v1.
- Product Policy and chat policy changes are editable in v1 through
  `OperatorCommandService`.
- `config_yaml` fields are readonly in v1 unless a future `ConfigCommandService`
  or equivalent command facade owns the write path.
- Fields hidden in v1 may still be documented here when their product status
  matters.

## Product Policy Fields

Product Policy fields are runtime product settings. They are editable in v1 and
write to Product Policy Store through policy command routes.

### Global Policy

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `policy.global.p2p_auto_reply` | P2P auto reply | `product_policy_store` | `normal` | yes | no | `policy_audits` | `PATCH /api/policy/global` |
| `policy.global.unknown_group_auto_reply` | Unknown group auto reply | `product_policy_store` | `normal` | yes | no | `policy_audits` | `PATCH /api/policy/global` |
| `policy.global.default_bot_joined` | Default bot joined | `product_policy_store` | `advanced` | yes | no | `policy_audits` | `PATCH /api/policy/global` |
| `policy.global.default_reply_identity` | Default reply identity | `product_policy_store` | `advanced` | yes | no | `policy_audits` | `PATCH /api/policy/global` |
| `policy.global.default_allow_user_fallback` | Default user fallback | `product_policy_store` | `advanced` | yes | no | `policy_audits` | `PATCH /api/policy/global` |
| `policy.global.default_resource_download` | Default resource download | `product_policy_store` | `advanced` | yes | no | `policy_audits` | `PATCH /api/policy/global` |

Help notes:

- `unknown_group_auto_reply`: Explain that this applies to groups without an
  explicit chat policy.
- `default_reply_identity`: Explain `bot_preferred`, `bot`, and `user` in
  product language.
- `default_allow_user_fallback`: Explain that it only matters when reply identity
  can prefer the bot but fall back to the user.

### Chat Policy

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `policy.chat.name` | Chat name | `product_policy_store` | `normal` | yes | no | `policy_audits` | `PATCH /api/policy/chats/{chat_id}` |
| `policy.chat.auto_reply` | Auto reply | `product_policy_store` | `normal` | yes | no | `policy_audits` | `PATCH /api/policy/chats/{chat_id}` |
| `policy.chat.bot_joined` | Bot joined | `product_policy_store` | `normal` | yes | no | `policy_audits` | `PATCH /api/policy/chats/{chat_id}` |
| `policy.chat.reply_identity` | Reply identity | `product_policy_store` | `normal` | yes | no | `policy_audits` | `PATCH /api/policy/chats/{chat_id}` |
| `policy.chat.allow_user_fallback` | Allow user fallback | `product_policy_store` | `normal` | yes | no | `policy_audits` | `PATCH /api/policy/chats/{chat_id}` |
| `policy.chat.resource_download` | Resource download | `product_policy_store` | `normal` | yes | no | `policy_audits` | `PATCH /api/policy/chats/{chat_id}` |

Help notes:

- `bot_joined`: Explain that bot-based replies and resource access depend on the
  bot being available in the chat.
- `resource_download`: Explain that this controls saving downloadable message
  resources for processing context.

### Policy Status And Audit

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `policy.status.initialized` | Product Policy initialized | `runtime_status` | `readonly` | no | no | none | none |
| `policy.status.import_diff` | Policy Import Diff | `derived` | `readonly` | no | no | none | none |
| `policy.audit.history` | Policy audit history | `product_policy_store` | `readonly` | no | no | read only | none |
| `policy.import_config` | Import config policy | `config_yaml` | `normal` | command | no | `policy_audits` | `POST /api/policy/import-config` |

`policy.import_config` is a command, not a persistent setting. It imports policy
fields from the Policy Import Source into Product Policy Store.

## Lifecycle Settings

Lifecycle fields affect operator workflow, but they currently live in
`config.yaml`. They are console-exposed, but readonly in v1.

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `lifecycle.approval_timeout_hours` | Approval timeout | `config_yaml` | `normal` | no | yes | future config audit | future `ConfigCommandService` |
| `lifecycle.watch_minutes` | Watch window | `config_yaml` | `normal` | no | yes | future config audit | future `ConfigCommandService` |
| `lifecycle.closed_recall_days` | Closed recall window | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |

Help notes:

- `approval_timeout_hours`: Explain that pending approvals become overdue in
  read models before explicit expiry moves them to expired.
- `watch_minutes`: Explain how long a task remains active after activity.

## Retention Settings

Retention fields are product-relevant local storage settings. They are readonly
in v1 because the console does not write `config.yaml`.

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `retention.raw_message_days` | Raw message retention | `config_yaml` | `normal` | no | yes | future config audit | future `ConfigCommandService` |
| `retention.resource_days` | Resource retention | `config_yaml` | `normal` | no | yes | future config audit | future `ConfigCommandService` |

## Runtime And Daemon Settings

These settings affect daemon cadence and health behavior. Keep them advanced and
readonly in v1.

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `daemon.tick_interval_seconds` | Daemon tick interval | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `daemon.overlap_seconds` | Message overlap window | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `health.interval_seconds` | Health refresh interval | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `health.retry_interval_seconds` | Health retry interval | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `health.timeout_seconds` | Health timeout | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |

## Storage And Quota Settings

Paths are installation concerns. Quotas are product-relevant but still readonly
in v1.

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `storage.sqlite_path` | SQLite path | `config_yaml` | `diagnostic` | no | yes | none | none |
| `storage.resource_dir` | Resource directory | `config_yaml` | `diagnostic` | no | yes | none | none |
| `storage.max_resource_bytes` | Max resource size | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `storage.max_resource_dir_bytes` | Max resource directory size | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |

## Agent Backend Settings

Agent backend settings are advanced because they affect processing behavior and
runtime availability.

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `agent_backend.provider` | Agent provider | `config_yaml` | `readonly` | no | yes | none | none |
| `agent_backend.working_dir` | Agent working directory | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.config_scope` | Agent config scope | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.auto_context` | Agent auto context | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.explicit_context.skills` | Explicit skills | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.hermes.mode` | Hermes health mode | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.hermes.model` | Hermes model | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.hermes.provider` | Hermes provider | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.hermes.router_max_turns` | Router max turns | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.hermes.session_max_turns` | Session max turns | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.hermes.timeout_seconds` | Hermes timeout | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `agent_backend.hermes.path` | Hermes executable | `config_yaml` | `diagnostic` | no | yes | none | none |
| `agent_backend.hermes.health_url` | Hermes health URL | `config_yaml` | `diagnostic` | no | yes | none | none |
| `agent_backend.hermes.api_key_env` | Hermes API key env | `config_yaml` | `diagnostic` | no | yes | none | none |
| `tool_permissions` | Tool permissions | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |

## Feishu And Owner Settings

Owner and CLI path fields are mostly identity and installation diagnostics. They
should not be ordinary editable controls in v1.

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `owner.open_id` | Owner open ID | `config_yaml` | `readonly` | no | yes | none | none |
| `owner.name` | Owner name | `config_yaml` | `readonly` | no | yes | none | none |
| `lark_cli.path` | lark-cli executable | `config_yaml` | `diagnostic` | no | yes | none | none |
| `lark_cli.timeout_seconds` | lark-cli timeout | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |

## Logging And Debug Settings

Logging fields are diagnostic by default. Debug fields should stay hidden unless
a future diagnostics workflow deliberately exposes them.

| key | label | source | visibility | editable_v1 | requires_restart | audit_behavior | write_boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `logging.jsonl_path` | JSONL log path | `config_yaml` | `diagnostic` | no | yes | none | none |
| `logging.level` | Log level | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `logging.console` | Console logging | `config_yaml` | `advanced` | no | yes | future config audit | future `ConfigCommandService` |
| `logging.text_path` | Text log path | `config_yaml` | `diagnostic` | no | yes | none | none |
| `debug.save_full_agent_io` | Save full agent I/O | `config_yaml` | `hidden` | no | yes | future config audit | future diagnostics command |

## UI Behavior

Normal Settings should show product-relevant fields first:

```text
Product Policy
Chat Policy
Approval and task lifecycle
Retention
```

Advanced Settings should group operational controls:

```text
Daemon and health
Storage quotas
Agent backend
Tool permissions
Logging
```

Diagnostics should show installation and runtime facts:

```text
Owner identity
SQLite/log/resource paths
lark-cli and Hermes executables
Health URL and API key environment variable name
```

Hidden fields should not appear in the default console. If a future diagnostics
mode exposes them, it must explain the operational consequence inline and still
write through an explicit command boundary.

## Route Expectations

`GET /api/settings/catalog` should return the catalog metadata for
console-exposed fields.

`GET /api/settings/runtime` should return current readonly values and derived
runtime status for settings screens.

Policy values may also be returned through dedicated policy routes. The renderer
may join policy status, policy values, and catalog metadata client-side, but it
must not infer hidden write behavior that is absent from the catalog.
