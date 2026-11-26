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

准备独立的飞书测试应用和测试群，订阅 `im.message.receive_v1` 并设置长连接。飞书应用身份需有 `im:message.group_msg`，以便后台任务在话题内查找回复位置。确认其他接收方没有连接同一个应用，然后创建空 profile：

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
platform_toolsets:
  feishu: [web, browser, vision, terminal, file, skills, clarify, delegation, cronjob, feishu_doc, feishu_drive]
  cron: [web, browser, vision, terminal, file, skills, delegation, feishu_doc, feishu_drive]
platform_hints:
  feishu:
    replace: 飞书答复支持 Markdown。需要随答复发送本地附件时，使用 MEDIA:/absolute/path/to/file。
agent:
  disabled_toolsets: [session_search, code_execution, computer_use, connections, image_gen, kanban, memory, todo, tts]
  tool_use_enforcement: false
  execution_guidance: false
  task_completion_guidance: false
  environment_probe: false
  bot_mode_protocol: false
```

父会话使用 `gpt-6-luna`，原生子 Agent 默认继承该模型。测试群成员通过 @ 触发；引用机器人回复时也 @ 机器人。`group_allowed_chats` 授权该群，`group_rules` 控制群成员及 @ 触发，`admins` 留空以免绕过群规则。需要限制特定群成员时，可将该群的 `policy` 改为 `allowlist` 并列出成员 ID。私聊沿用 Hermes 的一对一会话，用 `/new` 切分。需要跨会话回忆时，先确定群聊和私聊在该 profile 内的可见范围。定期检查使用 Hermes 原生 `/loop`。

Hermes v0.23.2 需要[飞书话题与去重补丁](../patches/hermes-0.23.2-feishu-topic-replies.patch)。后台结果缺少回复目标时，适配器使用话题内最近的有效消息作为锚点。同一次回复重试复用锚点和 UUID。Gateway 投递账本把结果不明的 Feishu 回复留待核对，并在日志中记录。核对飞书上的实际结果后再决定是否手工重发。

飞书适配器保留原生合批。消息在入队提交或被 Gateway 消费前只在进程内去重，因而重启后能接收飞书对这些消息的重投。恢复仍依赖飞书重投尚未持久入队的事件。若进程在入队提交后、适配器去重文件写盘前退出，飞书单独重投未被原生入队键覆盖的成员时，仍可能重复执行。旧版本已经写入的去重记录按原保留期过期。

补丁基于未修改的上游 `v0.23.2` 标签。确认以下版本检查输出 `v0.23.2` 后再应用。升级到其他版本后，重新检查上游的回复与入队行为并运行对应的 Gateway 测试。

```bash
git -C /path/to/hermes-agent describe --tags --exact-match HEAD
git -C /path/to/hermes-agent apply --check /path/to/feishu-shadow-agent/docs/patches/hermes-0.23.2-feishu-topic-replies.patch
git -C /path/to/hermes-agent apply /path/to/feishu-shadow-agent/docs/patches/hermes-0.23.2-feishu-topic-replies.patch
```

运行中收到带引用的追加消息时，Hermes 的 steer/redirect 还需要保留引用对象；`/steer` 回退排队时也需保留引用字段。本项目参考上游 [PR #118296](https://github.com/NousResearch/hermes-agent/pull/118296) 提供[兼容补丁](../patches/hermes-0.23.2-busy-reply-context.patch)，包含适配当前版本的实现和回归用例。该补丁在已应用上述话题补丁的 `f5765e51` 上验证；升级后先确认上游是否已修复，避免重复应用。补丁中的 Hermes 代码遵循其 [MIT 许可证](../patches/hermes-agent-MIT.txt)。

这不覆盖 Agent 尚在初始化时收到的追加消息：此时补充内容可能作为下一轮排队，造成两次答复。上游跟踪见 [issue #154296](https://github.com/NousResearch/hermes-agent/issues/154296)；不要把运行中引用补丁视为该问题的修复。

```bash
git -C /path/to/hermes-agent apply --check /path/to/feishu-shadow-agent/docs/patches/hermes-0.23.2-busy-reply-context.patch
git -C /path/to/hermes-agent apply /path/to/feishu-shadow-agent/docs/patches/hermes-0.23.2-busy-reply-context.patch
```

## 按需加载技能

Hermes v0.23.2 的技能索引默认要求加载部分相关的技能。[可选补丁](../patches/hermes-0.23.2-on-demand-skills.patch)保留技能索引和 `skill_view`，允许按 profile 和平台将这段说明改为按需加载；未配置的平台仍沿用原文。确认补丁适用当前版本后，在所需 profile 中设置：

```yaml
skills:
  prompt_guidance:
    feishu: on_demand
```

补丁基于 `f5765e51`，升级 Hermes 时需重新核对。运行中的网关不会自动加载修改过的 Python 代码；维护窗口重启共享网关后，新会话使用新代码，已有会话需按下文刷新提示词。其他 profile 和未配置的平台保留原有技能说明。

## 飞书历史检索

需要机器人根据转发内容补全工作上下文时，将 `lark-im` 与 `lark-shared` 的技能目录合并到 `coworker` profile 的 `skills.external_dirs`。在 profile 的 `platform_hints.feishu.append` 说明：按需检索并核实上下文；经飞书身份校验的 owner 私聊可使用 `--as user`，其他场景按授权选择查询身份；不能确定来源或遇到授权错误时如实说明。技能注册和提示词约束不等于权限硬隔离，终端完全访问模式下也不能阻止 Agent 自行访问主机可读的凭据。

`lark-cli` 在 Hermes 环境中使用 Hermes 绑定的飞书应用，其用户授权与其他 CLI 应用独立。按 `lark-shared` 的身份和授权流程，为该应用授予消息搜索、消息只读、个人与群聊历史及会话只读所需 scope；需要可续期时检查 `offline_access` 与登录结果中的 refresh-token 状态。不得复制其他应用的 token，也不要把密钥或登录结果写入仓库。配置技能或提示词后，可运行 `hermes --profile coworker sessions refresh-prompts --json`，让既有会话在下次对话时重建提示；运行中 Gateway 的生效状态仍需检查。

检查授权结果时，应核对实际生效的 scope，而不是只看发起登录时申请的范围。真实私聊测试中，部分文本转发在接收事件中表现为普通文本，读取到的转发副本也未包含来源会话字段；需要通过原文检索或向用户确认来源。旧会话中的既有话题和回答仍可能影响模型，即使检索提示已生效也应验证实际结果。

需要控制回复风格时，可在 profile 的 `SOUL.md` 中直接写明语言、场景和语气，并保留按需使用的写作技能；这不会增加独立的二次改写调用，也不保证模型每次都完全遵循。

## 启停与操作台

```bash
uv run --locked python -m feishu_shadow_agent coworker run --runtime hermes
hermes -p coworker gateway status
hermes -p coworker gateway stop
hermes -p coworker dashboard --host 127.0.0.1 --port 9119 --no-open
```

项目 Console 的「AI 同事」页提供「打开 Hermes Dashboard」，在新标签页打开 `127.0.0.1:9119` 并预选 `coworker`。需要远程操作时，使用 SSH 转发 9119。Hermes 的 Dashboard、CLI、TUI 或 Desktop 显示和续接其原生会话。

项目启动入口不接受 `--config`、`--mode` 或 `--reply-mode`；这些设置由 Hermes profile 管理。`SOUL.md` 定义飞书同事的职责、资料范围和协作方式，新会话读取该文件。建议保持简短，把可选流程放在按需读取的技能中。飞书会话使用 `platform_toolsets.feishu`。独立定时任务默认使用 `platform_toolsets.cron`，任务自选工具组仍受 `agent.disabled_toolsets` 限制。技能索引可通过 `skills.disabled` 精简。

Hermes 默认使用 `display.busy_input_mode: interrupt`。需要后续输入依次执行时，可在 profile 中设为 `queue`。飞书撤回事件目前由适配器记录，已入队的快照和已运行的任务由 Gateway 管理。可用原生 `/stop` 终止当前会话的工作。

Hermes Gateway 没有项目级 dry-run 开关，项目启动入口对 Hermes 的 `--dry-run` 返回错误。验证启动命令可先使用无飞书凭据的隔离 profile 和模拟网关；连接真实应用后，网关可能自动答复，须单独安排实际发送验收。
