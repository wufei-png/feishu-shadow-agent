# P5 Policy And Routing Plan

## Summary

P5 收紧并实现产品策略语义，不做 DB 大迁移。目标是把 chat policy、resource policy 和 reply gate 统一到 Python-owned `PolicyResolver`，同时把 P2P single-active-task 从硬规则降级为语义路由。

## Goals

- 用 `reply_policy.unknown_group_auto_reply` 取代 `default_group_auto_reply`。
- 对 unknown group 真正支持全局自动回复开关。
- 新增 `PolicyResolver`，集中 chat policy 派生、资源下载策略、回复身份和 auto-reply gate。
- 保持 `resource_download`、`bot_joined`、`allow_user_fallback` 的变量职责独立。
- 移除 P2P single-active deterministic attach。

## Non-goals

- 不修改 `tool_permissions` 默认值。
- 不引入 per-chat lifecycle。
- 不重写 task lifecycle schema；P6 负责。
- 不改变 Hermes output schema。
- 不做 SDK 或第二 agent backend。

## Config Changes

移除：

```yaml
reply_policy:
  default_group_auto_reply: false
```

新增：

```yaml
reply_policy:
  unknown_group_auto_reply: false
```

旧字段不兼容。配置中出现 `default_group_auto_reply` 应因 Pydantic `extra="forbid"` 失败。

## Policy Semantics

显式配置群：

```text
auto_reply = chats.<chat_id>.auto_reply
resource_download = chats.<chat_id>.resource_download
bot_joined = chats.<chat_id>.bot_joined
reply_identity = chats.<chat_id>.reply_identity
allow_user_fallback = chats.<chat_id>.allow_user_fallback
```

未知群：

```text
auto_reply = reply_policy.unknown_group_auto_reply
resource_download = ChatPolicyConfig.resource_download default
bot_joined = ChatPolicyConfig.bot_joined default
reply_identity = ChatPolicyConfig.reply_identity default
allow_user_fallback = ChatPolicyConfig.allow_user_fallback default
```

默认值保持开放，不额外引入 unknown group 特判：

```text
resource_download default remains true
bot_joined default remains false
reply_identity default remains bot_preferred
allow_user_fallback default remains true
unknown_group_auto_reply default is false
```

因此，当 operator 显式设置：

```yaml
reply_policy:
  unknown_group_auto_reply: true
```

未知群在现有 gate 通过时可以自动回复；如果 bot 不在群且 `bot_preferred` 不能用 bot，默认允许 user fallback。

## PolicyResolver Boundary

新增模块建议：

```text
src/feishu_shadow_agent/policy.py
```

`PolicyResolver` 负责：

- `resolve_chat_policy(chat_id, chat_type)`
- `can_download_resources(message/chat)`
- `resolve_reply_policy(task, message, composed, answerability)`
- 返回 `allow`、`reason`、`identity`、`policy_source` 等可审计字段

`PolicyResolver` 不负责：

- 生成 Hermes `answerability`
- 校验 Hermes schema
- 清理 reply text 或 mention
- 创建 approval
- 创建 action
- 执行 Feishu 发送

## Routing Semantics

保留 deterministic shortcut：

```text
reply_to msg key uniquely matches one active task
thread key uniquely matches one active task
```

移除 deterministic shortcut：

```text
P2P chat has exactly one active task
sender key matches an active task without structural evidence
```

P2P 新消息规则：

```text
if active candidates exist:
  call TaskRouter and allow new_task | attach_task | ambiguous | ignore
else:
  create new_task
```

代码仍然负责候选范围和 target validity；TaskRouter 只在 Python 提供的候选和 route allowlist 内做语义判断。

## Files To Update

- `src/feishu_shadow_agent/config.py`
- `src/feishu_shadow_agent/processing.py`
- `src/feishu_shadow_agent/ingestion.py`
- `src/feishu_shadow_agent/routing.py`
- `config.example.yaml`
- `schemas/config.schema.json`
- `docs/configuration.md`
- `docs/specs/feishu-shadow-agent-mvp-design.md`
- `tests/fixtures/minimal.config.yaml`
- `tests/test_config.py`
- `tests/test_p2_ingestion_routing.py`
- `tests/test_p3_hermes_approval.py`

## Test Plan

- Config tests:
  - `unknown_group_auto_reply` default is `false`.
  - `default_group_auto_reply` is rejected.
  - schema fixture matches generated schema.
- Policy tests:
  - explicit chat policy overrides unknown-group setting.
  - unknown group with `unknown_group_auto_reply=false` downgrades to approval.
  - unknown group with `unknown_group_auto_reply=true`, `bot_joined=false`, default `allow_user_fallback=true` can create user auto-reply action when all other gates pass.
  - resource download uses `resource_download` independently from `unknown_group_auto_reply`.
- Routing tests:
  - P2P single active task no longer attaches deterministically.
  - P2P single active task with semantically same task can attach only through TaskRouter.
  - P2P unrelated topic can become `new_task` through TaskRouter.
  - `reply_to` and `thread` unique matches still deterministic.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_config.py tests/test_p2_ingestion_routing.py tests/test_p3_hermes_approval.py
.venv/bin/python -m pytest -q
git diff --check
```
