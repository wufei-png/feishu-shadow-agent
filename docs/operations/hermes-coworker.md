# Hermes Gateway AI 同事

Hermes Gateway 使用独立的 `coworker` profile，负责飞书连接、会话和原生子 Agent；项目的 `coworker run --runtime hermes` 只调用 `hermes -p coworker gateway start`，不加载项目配置或数据库。旧的 `daemon` 搭配 `coworker.provider: hermes` 仍使用项目接入和单轮 Hermes 调用。

## 升级与迁移

预检最新稳定版、现有 profile 和服务状态。备份 Hermes 数据及原服务配置：

```bash
hermes --version
hermes update --check
hermes profile list
systemctl --user show hermes-gateway.service hermes-gateway-wechatbot.service \
  --property=Id,LoadState,ActiveState,SubState,UnitFileState,MainPID --no-pager
```

旧版本可能没有 `gateway migrate`。升级前在默认 profile 的 `config.yaml` 设置 `gateway.auto_multiplex_migration: false`，防止更新自动合并服务；也可使用隔离安装的新版 Hermes，对现有 `HERMES_HOME` 执行以下预检。升级到支持多 profile 主网关的稳定版后，执行预检并检查凭据冲突与服务边界：

```bash
hermes gateway migrate --multiplex --dry-run
hermes gateway migrate --multiplex
```

预检发现重复的平台凭据时，为各 profile 配置独立应用，或只保留一处凭据并按 Hermes 的 `profile_routes` 路由。迁移会停止并卸载独立的次级网关服务、更新主 profile 配置并重启主网关；Hermes 在 `~/.hermes/gateway_migration.json` 记录迁移状态。备份用于按原版本、配置和服务恢复；检查具体迁移记录后再操作。详见 [Hermes 多网关迁移](https://hermes-agent.nousresearch.com/docs/user-guide/multi-profile-gateways#migrating-from-per-profile-gateways)。

## 创建 profile

准备独立的飞书测试应用和测试群，订阅 `im.message.receive_v1` 并设置长连接。确认其他接收方没有连接同一个应用，然后创建空 profile，不复制现有应用凭据：

```bash
hermes profile create coworker --no-alias --no-credentials
install -m 600 /dev/null "$HOME/.hermes/profiles/coworker/gateway.parked"
hermes -p coworker gateway stop
hermes -p coworker gateway status
hermes gateway list
```

停放标记在主网关未运行时也生效；若主网关刚接管过该 profile，等待它停止服务并检查列表。自定义 `HERMES_HOME` 时，标记放在该目录的 `profiles/coworker/` 下。确认停放后再配置：

```bash
hermes -p coworker gateway setup
hermes -p coworker model
```

通过 Hermes 配置工具在 `coworker` profile 中设置应用 ID、密钥、`FEISHU_DOMAIN=feishu`、`FEISHU_CONNECTION_MODE=websocket` 和 `FEISHU_ALLOWED_USERS=ou_owner`，保持 `FEISHU_ALLOW_ALL_USERS` 和 `GATEWAY_ALLOW_ALL_USERS` 关闭。若事件还包含同一 owner 的 `user_id`，将它也加入白名单，例如 `FEISHU_ALLOWED_USERS=ou_owner,u_owner`；Hermes Gateway 优先用 `user_id` 做授权。不要将凭据写入本仓库。将以下内容合并到该 profile 的 `config.yaml`，替换工作目录和测试群：

```yaml
model:
  default: gpt-6-luna
  provider: openai-codex
terminal:
  cwd: /path/to/workspace
group_sessions_per_user: false
thread_sessions_per_user: false
platforms:
  feishu:
    enabled: true
    extra:
      default_group_policy: disabled
      admins: []
      group_allowed_chats: [oc_test_group]
      group_rules:
        oc_test_group:
          policy: open
          require_mention: true
delegation:
  gateway_mode: background
display:
  platforms:
    feishu:
      tool_progress: "off"
      interim_assistant_messages: false
memory:
  memory_enabled: false
  user_profile_enabled: false
agent:
  disabled_toolsets: [session_search]
```

父会话使用 `gpt-6-luna`，原生子 Agent 默认继承该模型。测试群成员通过 @ 触发；引用机器人回复时也 @ 机器人。`group_allowed_chats` 授权该群，`group_rules` 控制群成员及 @ 触发，`admins` 留空以免绕过群规则。需要限制特定群成员时，可将该群的 `policy` 改为 `allowlist` 并列出成员 ID。私聊沿用 Hermes 的一对一会话，用 `/new` 切分。需要跨会话回忆时，先确定群聊和私聊在该 profile 内的可见范围。定期检查使用 Hermes 原生 `/loop`。

## 启停与操作台

```bash
uv run --locked python -m feishu_shadow_agent coworker run --runtime hermes
hermes -p coworker gateway status
hermes -p coworker gateway stop
hermes -p coworker dashboard --host 127.0.0.1 --port 9119 --no-open
```

需要远程操作时，使用 SSH 转发本机端口。Hermes 的 Dashboard、CLI、TUI 或 Desktop 显示和续接其原生会话；项目 Console 显示项目持有的状态。

Hermes Gateway 没有项目级 dry-run 开关，项目启动入口对 Hermes 的 `--dry-run` 返回错误。验证启动命令可先使用无飞书凭据的隔离 profile 和模拟网关；连接真实应用后，网关可能自动答复，须单独安排实际发送验收。
