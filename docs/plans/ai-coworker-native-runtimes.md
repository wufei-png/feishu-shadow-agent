# AI 同事原生运行时实施方案

2026-11-19，待实现。当前实现和操作步骤见 [AI 同事运行与验证](../operations/single-group-ai-coworker.md)。

## 运行时边界

| 启动方式 | 飞书、会话和操作界面的归属 |
| --- | --- |
| Codex 直连 | 项目接收飞书事件、绑定话题与回复、发布答复。Codex App Server 管理执行、历史、上下文压缩和原生子 Agent。项目 Console 显示接入与会话状态。 |
| Hermes Gateway | 现有 Hermes 主网关承载单独的 `coworker` profile。Hermes 管理飞书、会话、原生 `delegate_task` 和记忆配置。操作用 Hermes 自带 Dashboard、CLI 或 Desktop。 |

新增同一个启动入口，建议为 `coworker run --runtime codex|hermes`。Codex 沿用现有 daemon、启动检查和 `--dry-run`。Hermes 选择已配置的 profile，交给原生 `hermes -p coworker gateway start`，由主网关管理生命周期。该分支无需打开项目 SQLite。保留旧 `daemon` 入口及其 `coworker.provider: hermes` 的单轮调用语义。首次联调用独立飞书应用。同一应用切换运行时前先确认原接收方已断开。 [飞书长连接投递规则](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-) · [Hermes 单 profile 启停](https://hermes-agent.nousresearch.com/docs/user-guide/multi-profile-gateways#stopping-one-profile-without-stopping-the-host)

## Codex 直连

### 会话、话题与发布

- 普通群主区共享一个原生会话，真实话题各有一个。保留已有的 UUID 和工作目录绑定。群内 @ 或引用机器人回复都可触发，目标话题以飞书实际位置和已确认的回复绑定为准。群主区贴话题链接时，Agent 按需读取该话题，答复仍落在当前对话。用实际话题位置区分 `thread_id` 关联的根消息和话题内消息，普通引用的 `root_id` 单独不能决定路由。
- `--reply-mode quote|topic|auto` 控制群主区的自动发布位置，默认 `quote`。`quote` 引用原消息，`topic` 为新请求开话题，`auto` 由 Agent 判断。短问答引用回复，可以一次完成但篇幅较长的答复可直接用结果开启话题。明确要求开题，或 Agent 在 `auto` 下判断宜在话题里持续执行时，先发简短接单消息，确认飞书话题并绑定，再在独立的 Codex 会话里执行和委派。`auto` 复用主区的正常模型轮做决策，只有需要移交时才在话题开启执行轮。已在话题里的答复留在原话题。
- 新话题仅带入原请求、必要的引用和与本任务直接相关的进展。Codex 原生 `thread/start` 足以实现这一点。原生 `thread/fork` 会复制选定节点之前的完整会话历史，适合显式继承整段上下文的场景。 [Codex App Server 会话接口](https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread)
- Codex 自主决定如何执行、委派、澄清及组织答复。适配层发布本轮最终答复并登记回执，复用飞书客户端、消息队列和同事会话记录。普通的已授权回复采用独立的轻量发布路径，以机器人身份在已授权的群或 owner 私聊发布。发送回执用于续接、幂等和核对。开题回执缺少话题 ID 时按返回的消息 ID 定向回查。发送结果不确定时保留待核对状态，避免重跑模型或改用新幂等键重发。工具轨迹和中间 commentary 留在原生会话。 [Lark CLI 回复实现](https://github.com/larksuite/cli/blob/c5fbf1a52df77d5cb9cac2b1501770b2fb9af7d2/shortcuts/im/im_messages_reply.go#L215-L237)
- 后台子 Agent 完成后唤醒实际拥有它的原生会话，按原请求和已绑定的话题汇报。并发请求或复用同一个子 Agent 时，用本次子轮的来源定位。补充信息仅在属于当前工作的情况下送入正在运行的轮次，其他请求排队。区分补充已被 App Server 接收和主轮已完成。

### 关注、私聊与人工接手

- **关注。** 仅 owner 可以明确开启或取消指定的真实话题关注。默认 24 小时，可指定期限。复用现有飞书入站和话题队列，合并密集消息为一次增量判断，只有相关变化才回原话题。到期或取消后，@ 和引用仍可续接。无 @ 的关注依赖相应事件权限，涉及其他机器人发言时单独验收。 [飞书消息事件权限](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/guide/faq)
- **私聊。** 仅接收 owner 与机器人的私聊。每次未引用的新请求开启独立会话，包括单条和合并转发；引用对应的转发或机器人回复续接。转发内容保留可取得的原文与来源，目标不清楚时由 Agent 问一句。回复保持在该私聊，默认不向转发来源发送消息。明确要求记住时写入私聊专用笔记。群整理只使用群资料，私聊轮次不进入共享的原生记忆积累。实现前用实际 Codex 版本核实关闭共享记忆读写的生效方式，并覆盖首次启动和恢复。 [Codex 本地记忆](https://learn.chatgpt.com/docs/customization/memories)
- **人工接手。** 暂停选中会话的自动领取和 steering，继续接收入队；等主轮和原生子 Agent 收尾，确认 App Server 已释放该线程。操作员在原工作目录运行 `codex resume <UUID>`。操作员退出并交还后，daemon 从原生存储重新加载同一 UUID，再处理积压。其他话题照常运行。Console 或 CLI 展示可验证的暂停、就绪、恢复状态和接手命令。 [App Server 线程释放](https://learn.chatgpt.com/docs/app-server#unsubscribe-from-a-loaded-thread)
- **上下文与提示词。** 每轮只提供身份、触发原因、当前消息、直接引用与所在位置，以及需要的通道指令。历史和记忆按需取用，沿用原生规则、skills 和压缩。恢复只在需要时加载，调用方不用读取完整历史时使用当前版本支持的精简 `thread/resume`。新配置默认关闭群记忆每日整理，保留显式写入及整理开关，已生效的 SQLite 设置照旧。 [App Server 恢复接口](https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread)

## Hermes Gateway

- 用现有主网关和一个全新的 `coworker` profile。运行手册给出最新稳定版预检、独立测试应用、原生启停和回退步骤。调研基线为 v0.23.2，更新可能合并旧的独立 systemd 服务，应先核对迁移计划。项目启动器只启动指定 profile，由 Hermes 负责安装、升级和服务管理。查进程用只输出状态字段的 `systemctl show`，联调记录不保存 SDK 日志中的连接票据。 [Hermes v0.23.2](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.2) · [多网关迁移](https://github.com/NousResearch/hermes-agent/blob/v0.23.2/website/docs/user-guide/multi-profile-gateways.md#L1151-L1169)
- 交付可复制的 profile 示例和运行手册。飞书凭据及 `FEISHU_ALLOWED_USERS` 通过 profile 的凭据配置提供，私聊只列 owner。工作目录显式给出。保持 Hermes 原生运行时以使用 `delegate_task`，模型提供方可以独立选择。飞书群启用 @ 触发，已有话题按原生路由；群里仅引用回复时同时 @。私聊沿用 Hermes 的一对一会话，`/new` 切分。启用跨会话回忆前先考虑该 profile 内群聊和私聊的可见范围。 [飞书接入](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/feishu) · [原生后台委派](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation) · [会话搜索范围](https://hermes-agent.nousresearch.com/docs/user-guide/sessions#who-can-search-what)
- Dashboard 按需启动，默认回环监听，远程主机用 SSH 转发。用原生界面看连接、会话和工具活动，支持时用 TUI、Desktop 或 Webapp 接续 Hermes 会话。Codex Console 仅展示项目持有的路由、发布和接手状态。两条路径用同一组业务场景验收。周期检查用 Hermes 原生 `/loop`，Codex 飞书话题关注使用事件触发。 [Web Dashboard](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard) · [Hermes 浏览器工作区](https://hermes-agent.nousresearch.com/docs/user-guide/desktop#use-the-desktop-workspace-from-a-browser) · [Hermes `/loop`](https://hermes-agent.nousresearch.com/docs/user-guide/features/loops)

推荐配置片段，合并到 `coworker` profile 的 `config.yaml`，替换工作目录、测试群和当前应用的用户 ID。凭据在同一 profile 通过原生设置工具配置。Feishu 中国站使用 `FEISHU_DOMAIN=feishu`，长连接使用 `FEISHU_CONNECTION_MODE=websocket`。

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

## 实现顺序

1. **打通启动和入站。** Hermes 先交付原生启动选择、推荐配置、操作台和验证说明。Codex 复用现有入队和会话绑定，先固定入站归一化与触发。增加已复现的 `mentions[].id` 为字符串时漏掉 @ 的回归用例。
2. **做完整的 Codex 会话和发布链路。** 从有回执的群引用开始，接上开题并移交、子 Agent 回投、owner 私聊和关注，再做轮间人工接续及最小状态展示。把业务决策交给原生 Agent。由项目保证路由、授权、队列和投递状态。答复保持自然文本，开题、移交和静默等通道动作只需要简短的结构化信号。
3. **迁移、验证和审查。** 如需扩展 SQLite，增量升级当前基线，保留现有会话、策略、审批和在途动作。新发布路径由新配置或明确选择启用，旧 `agent_direct` 和 `project` 保持原语义。已有宽泛 `followups` 不生成新的显式关注记录。对涉及的旧行为做回归，独立审查逻辑并在修复后复审。功能落地时同步更新现行运行和配置文档。

## 验收

两条路径分别验证启动、准入、话题内续接和延迟结果。Codex 另外验证开题策略、按转发开会话、事件触发关注和原目录接手。Hermes 按原生配置验收。

| 场景 | 通过条件 |
| --- | --- |
| 启动与准入 | 两个启动选项各自调用正确入口。Hermes 启动不碰项目数据库。两条路径均允许指定群的 @ 和 owner 私聊，拒绝其他群和非 owner 私聊。Codex 对未引用、未 @、未关注的群发言保持静默。 |
| 群引用、开题和跨话题引用 | 短答引用原消息。已有话题原地续接；耗时任务在飞书话题建立后才执行。别的话题链接只补上下文。并行任务和延迟结果回到自己的来源；重复事件不发送第二条答复。 |
| 私聊、关注和记忆 | 连续转发两件事后，引用第一条可继续第一件事。裸转发意图不清楚时能澄清。关注只看指定话题、随时可取消，到期停止触发。群和私聊记忆按配置和显式指令写入。 |
| 人工接手和恢复 | 暂停一个会话，终端用原目录和 UUID 续写，再交回。飞书新一轮能读到终端补充，其他话题继续运行。发送结果不明、任务中断或主轮失败时，状态不会误报完成。 |
| 兼容与真实联调 | 旧策略、待审批数据和在途发送保留约束。优先用假飞书、合成事件和隔离 App Server 验证。普通群创建话题、转发正文可见性、事件范围及 Hermes 回投在独立飞书应用中单独联调。 |

本地验证按 [仓库规范](../../AGENTS.md) 运行测试、静态检查和 `git diff --check`。触及 Console 时重建并验证前端。交付说明分别列出本地测试与实际飞书联调结果。
