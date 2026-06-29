# Feishu Shadow Agent MVP 设计

日期：2026-06-22

本文沉淀 `feishu-shadow-agent` MVP 的已确认设计。目标是做一个本机飞书个人助手：定时读取群聊里 `@我` 的消息和 P2P 私聊消息，交给 Hermes 处理，低风险高置信时自动回复，不确定或高风险时通过 bot 私聊 owner 审批。

流程图补充见 [Feishu Shadow Agent MVP 流程图](./feishu-shadow-agent-flows.md)，其中 [整体数据流程](./feishu-shadow-agent-flows.md#overall-flow) 可作为实现阅读入口。

## 1. MVP 边界

### 进入 MVP

- Python 长驻 `daemon/watch`。
- SQLite 存消息、任务、审批、动作、资源、checkpoint、运行日志。
- `lark-cli subprocess` 作为飞书主接入。
- Hermes CLI 非交互 `chat -q -Q` 作为 AI 处理引擎。
- 群聊 `@我` 和 P2P 私聊两条拉取入口。
- 命中消息的图片/文件资源下载。
- 通用 active task tracking。
- ApprovalRequest 审批队列。
- bot 私聊 owner 做通知和审批。
- 本地 CLI：`doctor`、`daemon`、`status`、`replay`、`approve`、`reject`、`send`、`config show --redacted`。
- JSONL 结构化日志和可审计动作记录。

### 不进入 MVP

- LaunchAgent / Windows service / Ubuntu systemd 守护安装。
- cron/短任务定时模式。
- 全群全量遍历。
- `chat-list` 作为主扫描路径。
- `@All` 处理。
- Web UI / 飞书交互卡片。
- 多 owner / 多租户。
- 自动配置变更。
- `config set`。
- `/reply <task_id> ...` 人类补充背景命令。
- running summary / context budget / 每次重复注入历史消息。

后续迭代可以做跨平台守护程序，覆盖 macOS、Windows、Ubuntu；也可以做本地 UI 审批台，复用同一套 ApprovalRequest 和 Store API。

## 2. 技术栈

MVP 锁定：

```text
Python + SQLite + lark-cli subprocess
```

保留接口抽象：

```text
FeishuClient
  LarkCliClient       # MVP
    shortcut path     # 默认使用 im +messages-* 等 shortcut
    raw api fallback  # shortcut 权限预检或 CLI bug 时兜底
  LarkSdkClient       # 后续需要脱离 CLI/keychain 时再做
```

不上 SDK 作为第一版主路径。当前 `lark-cli --as user` 已验证可读消息、搜消息、补上下文、回复消息；第一版难点在任务状态机、Hermes 决策、审计和幂等，不在 OAuth token 生命周期。

当前验证版本为 `lark-cli 1.0.56`。实现不硬编码小版本，只依赖 health check 验证实际命令能力；设计应兼容 `lark-cli` 小版本差异。

`LarkCliClient` 默认走 shortcut：

```text
im +messages-search
im +chat-messages-list
im +threads-messages-list
im +messages-reply
im +messages-resources-download
```

读取、搜索、上下文拉取和资源下载遇到 shortcut 权限预检、参数转换或 enrichment bug 时，可以 fallback 到 `lark-cli api` 原生 OpenAPI。对外发送回复不随意切换目标格式，仍保持 `messages-reply`、`--dry-run`、短 idempotency key 和发送后读回验证。

运行环境只做简要约束：daemon 启动时记录实际 `lark-cli` 路径和版本，并在 daemon 实际环境中验证 `PATH`、`HOME`、工作目录、日志路径和 `auth status --verify`。macOS 后台或非交互环境可能遇到 Keychain/cipher 问题，MVP 不提供守护安装，但必须通过 health check 暴露。

## 3. 运行形态

MVP 只做长驻 daemon：

```bash
python -m feishu_shadow_agent daemon
```

默认业务 tick：

```yaml
daemon:
  tick_interval_seconds: 60
  overlap_seconds: 120
```

每轮拉取窗口：

```text
start = last_success_at - overlap_seconds
end = now
```

使用 `message_id` 去重。

daemon 启动时运行完整、无副作用 health check suite，并发执行。critical fail 则 fail-closed，不启动。运行中 critical health check 失败时暂停 ingestion/sending，保持进程存活并周期重检。

health check 分级：

```text
critical:
  config schema
  SQLite writable
  lark-cli path/version
  auth status --verify
  required user scopes
  required bot basics
  Hermes CLI version/status reachable

warning:
  owner notification dry-run
  per-chat bot_joined/resource capability unknown
  optional scopes missing
```

必需 user scopes：

```text
search:message
im:message:readonly
im:chat:read
im:message.p2p_msg:get_as_user
im:message.group_msg:get_as_user
im:message.reactions:read
im:message.send_as_user
im:message
```

```yaml
health:
  interval_seconds: 300
  retry_interval_seconds: 60
```

## 4. 飞书身份边界

### user 身份

用于：

- 搜索和读取当前 owner 可见消息。
- 拉取上下文。
- P2P 自动回复。
- 人工确认后的代发回复。
- `/send` 手写最终回复。

### bot 身份

用于：

- owner 通知和审批私聊。
- 群聊自动回复首选身份。
- 下载消息图片/文件资源。

资源下载必须拆成两阶段：

```text
FeishuMessageReader       # --as user，搜索/读取消息/上下文
FeishuResourceDownloader  # --as bot，下载图片/文件资源
```

用户身份能读消息，不等于能下载消息里的资源。用户消息截图下载用：

```bash
lark-cli --as bot im +messages-resources-download \
  --message-id "$MESSAGE_ID" \
  --file-key "$IMAGE_KEY" \
  --type image \
  --output "./data/resources/..."
```

不要用 `/open-apis/im/v1/images/:image_key` 下载用户消息截图。

如果资源下载因为 bot 不在群失败，例如 `234040 The message is invisible to the operator`：

- 任务不自动回复。
- bot 私聊 owner，提示需要把 bot 拉进群。
- 记录待配置群。
- MVP 不做 owner bot DM `/retry`。

如果当前任务不依赖图片/文件等 bot-only 资源，bot 不在群不阻断任务处理。

资源读取、bot 入群 gate 与失败降级见 [资源下载与 bot gate](./feishu-shadow-agent-flows.md#resource-download-flow)。

## 5. 消息入口

MVP 只启用：

```text
group_at_me:
  lark-cli --as user im +messages-search --query "" --is-at-me --chat-type group ...

p2p:
  lark-cli --as user im +messages-search --query "" --chat-type p2p ...
```

不做：

- 所有群全量遍历。
- `chat-list` 主扫描。
- `@All` 处理。

`@All` 可能被 `--is-at-me` 命中，normalizer 需要区分：

```text
direct_mention
at_all
```

`at_all` 默认 suppressed，不自动回复，也不通知 owner。

normalizer 还必须标记消息发送者角色：

```text
external_user_message
owner_message
bot_message
agent_message
```

`bot_message` / `agent_message` 默认只入库和审计，不进入新任务匹配，不调用 TaskRouter，也不触发自动回复。发送后读回得到的 bot/agent 消息应关联到原 action/task，用于验证和审计，而不是作为新的 incoming work。

资源下载只针对进入处理流程的 root message 和必要上下文资源，不对整个上下文窗口全量下载。

## 6. Tick 顺序

每轮 tick 顺序：

```text
1. approval inbox
2. group_at_me ingest
3. p2p ingest
4. active task watch
5. pending actions dispatch
```

checkpoint 分开：

```text
checkpoint: ingest.group_at_me
checkpoint: ingest.p2p
checkpoint: approval_inbox
checkpoint: active_watch.chat.<chat_id>
checkpoint: active_watch.thread.<thread_id>
```

每个入口只在对应阶段“拉取 + 入库 + 初步归属处理”成功后推进 checkpoint。

ingest 分页必须 drain 完再推进 checkpoint：同一窗口内如果 `messages-search` / `chat-messages-list` / `threads-messages-list` 还有下一页，必须继续拉取并完成入库与初步归属。任何分页、入库或归属失败，都不推进该入口 checkpoint。

同一 chat/thread 在同一 tick 拉到多条消息时，按 `create_time asc, message_id asc` 逐条处理。每条消息完成 normalize、去重、归属和必要的 task 状态更新后，再处理下一条，避免后到消息先被错误 attach 到 active task。

daemon 单轮执行顺序见 [Daemon tick 流程](./feishu-shadow-agent-flows.md#daemon-tick-flow)。

## 7. 自动回复策略

默认策略：

```yaml
reply_policy:
  p2p_auto_reply: true
  unknown_group_auto_reply: false
```

含义：

- 不确定、涉及承诺/隐私/写操作/权限扩大/多人责任不清：bot 私聊 owner。
- P2P：证据完整、`answerability=auto_reply` 且回复 gate 通过时允许自动回复；single-active-task 只作为语义候选，不作为确定性 attach。
- 群聊：显式 per-chat policy 开启，或未知群 `unknown_group_auto_reply` 开启时，直接 `@我`、证据完整、全部确定性 gate 通过才自动回复。

身份规则：

```text
group auto_reply:
  preferred --as bot
  fallback --as user if bot unavailable and no missing resource evidence

p2p auto_reply:
  --as user

approved_reply:
  --as user

/send:
  --as user

owner notification / approval inbox:
  --as bot
```

所有对外回复都使用：

```bash
lark-cli im +messages-reply --message-id ...
```

不直接 `messages-send` 到群或 P2P。bot 给 owner 的通知除外。

所有发送前都执行 `--dry-run`，包括自动回复、`/approve`、`/send`。

Hermes 输出、reply policy gate 与发送动作创建见 [Hermes 处理与回复决策](./feishu-shadow-agent-flows.md#hermes-reply-flow)。

## 8. @ 用户规则

Hermes 不生成 @。Python `SendComposer` 统一加 `<at user_id="...">显示名</at>` 前缀。

规则：

- 默认 @ `reply_target_message.sender`。
- 只 @ 这一个人。
- 不 @All。
- 不复用原消息里所有 mentions。
- 如果目标发送者是 owner 自己或 bot，不 @。
- Hermes 输出中如包含 `<at ...>` 或 `@所有人`，Python 清理或降级审批。

发送文本示例：

```text
<at user_id="ou_xxx">张三</at> 建议先检查分类服务启动日志...
```

`lark-cli --text` 已实测会把 `<at>` 转为飞书结构化 mention。

mention 组装后的 dry-run、真实发送与读回验证见 [幂等发送与读回验证](./feishu-shadow-agent-flows.md#idempotent-send-flow)。

## 9. Per-chat policy

群聊自动回复从一开始按 per-chat policy 设计，MVP 可以用 YAML 配置，未来 UI 复用同一套 Store API。

MVP 最小字段：

```yaml
chats:
  oc_xxx:
    name: 示例产品群
    auto_reply: true
    bot_joined: true
```

预留完整模型：

```yaml
chats:
  oc_xxx:
    name: 示例产品群
    bot_joined: true
    auto_reply: false
    reply_identity: bot_preferred
    allow_user_fallback: true
    resource_download: true
```

未知群默认：

```text
处理但不自动回复；资源下载、bot_joined 和 user fallback 使用 ChatPolicyConfig 默认值。
```

P2P MVP 使用默认策略，不做 per-user 配置，但预留 `UserPolicyStore`。

## 10. Tool permission profile

工具权限和飞书对外回复权限分开。`tool_permissions` 只控制 Hermes CLI 运行时的工具权限，Python daemon 不解析、不执行 `tool_plan`。

```yaml
tool_permissions: guarded_write   # read_only | guarded_write | full_access
```

三档：

```text
read_only:
  Hermes 使用 --toolsets safe。
  safe 禁用 terminal/file/execute_code 等本地写操作类工具，但保留 web、vision、image_generate。
  不是严格意义的“零副作用只读”——image_generate 等仍可能产生文件。

guarded_write:
  Hermes 使用 --toolsets hermes-cli（不传 --yolo）。
  启用完整 CLI 工具集。daemon 以无 TTY 子进程调用 Hermes，不会触发交互式危险命令审批；
  非 gateway 场景下 Hermes 对 terminal 等路径通常自动放行。
  写风险主要靠 toolset 边界 + 本项目 JSON schema / reply gate / owner 审批，而非 Hermes TTY 确认。

full_access:
  Hermes 使用 --toolsets hermes-cli --yolo。
  显式跳过 Hermes 危险命令审批提示。
  仍受 Hermes hardline block、进程边界、系统权限和工具自身限制约束。
```

Python 只负责按 profile 派生 Hermes CLI 参数、记录审计、维护任务状态、执行飞书发送策略和幂等保护。

飞书对外回复不走 tool permission，统一由 reply policy、ApprovalRequest、dry-run、idempotency 和发送后读回管控。

## 11. Hermes 集成

Python daemon 直接调用 Hermes CLI 非交互 `chat -q -Q`。不要用 user 身份私聊飞书 bot 来启动 Hermes。

集成约束：

```text
任务处理:
  始终 hermes chat -q -Q（CLI 子进程），与 agent_backend.hermes.mode 无关。

Health 探测:
  agent_backend.hermes.mode: cli  -> hermes --version / hermes status
  agent_backend.hermes.mode: http -> 仍检查 CLI backend readiness，并追加 GET agent_backend.hermes.health_url（仅可达性，不用于 chat）

agent_backend.hermes.mode: http 不是 HTTP chat API；未来若接 Hermes API server，应单独设计客户端，不混用当前 health_url 字段。
```

职责拆分：

```text
Python daemon:
  编排器 / 状态机 / 审计 / 飞书收发

Hermes CLI non-interactive chat:
  每个任务的 AI 处理会话

Feishu bot DM:
  只做人类通知和审批入口
```

Hermes 会话：

```text
TaskRouter:
  无状态全新会话。
  只在复杂归属时调用。

Task Session:
  一任务一会话。
  新 task 初始 `agent_session_id = NULL`。
  首次 Task Session 成功后保存 agent backend 返回的真实 `session_id`。
  后续 follow-up 使用 agent backend 的 resume/session 机制；Hermes backend 对应 `hermes chat --resume <agent_session_id>`。
```

已有 `feishu-task-*` 旧值视为未初始化，迁移后清空。同一飞书 thread 后续消息可以挂到同一个 task session。

Hermes 输出必须是严格 JSON，由 Python schema 校验。校验失败降级 owner 审批。

## 12. Hermes 输入格式

Hermes 输入采用混合格式：

```text
system/developer 指令:
  角色、策略、输出 schema、禁止事项

metadata block:
  minimal JSON，只放 id、状态、策略和资源引用

context_access block:
  可选顶层 card，只在权限和本地 DB 条件允许时提供 read-only SQLite context

conversation block:
  简洁自然语言消息上下文，必须带 sender 信息
```

metadata 不提供关闭选项，MVP 固定 minimal：

```json
{
  "task_id": "t_xxx",
  "state": "watching",
  "chat_id": "oc_xxx",
  "root_message_id": "om_root",
  "current_message_id": "om_current",
  "resource_ids": ["res_1"],
  "policy_mode": "balanced"
}
```

Task Session 初次处理：

- root 消息。
- 必要上下文。
- 资源/图片。

Task Session follow-up：

- 只传新增消息。
- 只传新增资源。
- 不重复 root。
- 不重复历史关键消息。
- 不做 running summary。
- 不做 context budget。

follow-up 输入必须带发送者：

```text
[CURRENT_MESSAGE]
sender_name: 李四
sender_id: ou_xxx
message_id: om_xxx
create_time: 2026-06-22 10:12:30
text:
@张三 分类服务起不来，截图如下

attachments:
- data/resources/om_xxx/img_xxx.jpg
```

只有当 Hermes 明确表示上下文不足，或者会话恢复/丢失时，Python 才按 id 补发历史消息。

## 13. Hermes 输出

Task Session 输出负责回复草稿、回复目标和 watch 行为。`task_label` 只在首次 Task Session 输出并写入；follow-up schema 不包含也不更新 `task_label`。

Initial Task Session 示例 schema：

```json
{
  "task_label": "分类服务启动失败，用户反馈截图显示服务启动异常并伴随 500",
  "answerability": "auto_reply",
  "proposed_reply": "建议先检查分类服务启动日志...",
  "reply_target_message_id": "om_current",
  "watch_action": "keep_watching"
}
```

Follow-up Task Session 示例 schema：

```json
{
  "answerability": "auto_reply",
  "proposed_reply": "建议先检查分类服务启动日志...",
  "reply_target_message_id": "om_current",
  "watch_action": "keep_watching"
}
```

`reply_target_message_id` 不新增 LLM 调用，它是同一次 Task Session 输出字段。Hermes 只能从 Python 提供的候选里选：

```text
current_message_id
root_message_id
```

Python 校验 `reply_target_message_id` 必须在候选列表中。

`task_label`：

- 初次处理时由 Hermes 输出并写入任务。
- 限制约 100 个中文字符。
- 失败时 fallback 为 root message 清洗截断。
- follow-up 不接收 `task_label` 字段，也不会更新现有任务标签。

## 14. TaskMatcher 与 watch_keys

MVP 使用单一 `watch_keys` 模型，不使用多个参与者/锚点变量。

```text
watch_keys: set[str]
```

typed key：

```text
user:<open_id>
msg:<message_id>
thread:<thread_id>
```

候选 follow-up 规则：

```text
同一 chat/thread 内，如果满足任一条件：
  sender 的 user key 在 watch_keys
  reply_to 的 msg key 在 watch_keys
  message 的 thread key 在 watch_keys
  mentions 包含 owner
则进入候选，由 TaskMatcher 处理
```

`included_messages` 是任务消息关联表，只用于去重、审计、构建 Hermes 输入，不参与跟踪判断。

## 15. CandidateCollector 与 TaskRouter

统一 TaskMatcher 覆盖：

- active merge。
- 多次 @ 归并。
- 多人接力。
- 超时后的历史任务 recall。

流程：

```text
IncomingMessage
  -> normalize
  -> loop guard
  -> owner intervention check
  -> CandidateCollector 纯代码找候选 task
  -> 必要时 Hermes TaskRouter 一次无状态结构化判断
  -> new_task / attach_task / reopen_task / ignore / ambiguous
```

CandidateCollector 不调用 Hermes，只做 SQLite 检索。

loop guard 在 TaskRouter 前执行：

```text
bot_message / agent_message:
  入库和审计。
  如来自发送后读回，则关联原 action/task。
  写 routing audit：route=ignore, reason=self_message。
  不创建新任务。
  不进入 CandidateCollector。
  不调用 TaskRouter。

owner_message:
  不因 watch_keys 命中而作为普通外部 follow-up。
  先走 owner intervention check。
  若无法确定关联 active task，写 routing audit：
    route=ignore, reason=owner_message_not_task_intervention。
```

owner 在原 chat/thread 里直接回复 active task 相关消息时，视为人类接管，不作为普通 follow-up 送 Hermes，也不创建新任务。该判断在 CandidateCollector/TaskRouter 之前完成。

```text
owner_message:
  sender == owner
  非 owner 与 bot 的 approval inbox 命令

human_taken_over:
  owner_message 确定关联 active task
  例如 reply_to 命中 task_messages/agent reply，
  或 thread:<thread_id> 唯一命中 active task。
  P2P owner takeover 也必须有 reply_to 或 thread 结构性证据；
  同一 P2P chat 单 active task 不单独触发 takeover。
```

命中 `human_taken_over` 后：

- 记录 owner_intervention / route audit。
- 取消该 task 尚未发送的 `send_reply` action。
- 过期该 task 仍 pending 的 `send_reply` approval。
- 将 task 标记为 `human_taken_over`。
- 不再让 agent 自动回复这轮任务。

无法确定关联 task 的 owner 群消息只入库和审计，不创建任务、不调用 TaskRouter。

active candidates：

```text
same chat_id
now <= watch_until
watch_keys 命中，或当前消息 @ owner
```

historical candidates：

```text
仅新触发事件启用
same chat_id
最近 `lifecycle.closed_recall_days` 天，默认 7 天
Python 内部可用 sender/watch_keys/关键词/task_label/last_user_message/last_agent_reply 等存储摘要召回
```

TaskRouter 输出：

```json
{
  "route": "new_task|attach_task|reopen_task|ignore|ambiguous",
  "target_task_id": "t_xxx",
  "reason": "..."
}
```

结束、取消、已解决这类消息如果明确归属某个 active task，Router 仍输出 `attach_task`。任务是否关闭由后续 Task Session 的 `watch_action: "close"` 决定。

每次 route 都写入 match 决策审计：

```text
route_reason
candidates_count
shortcut_hit
router_called
matched_by
target_task_id
```

这些字段进入 actions/logs 或 task routing audit，供 `status`、`replay` 和故障排查使用。

可跳过 TaskRouter 的确定性 shortcut：

```text
群聊有 thread_id:
  thread:<thread_id> 唯一命中 active task。

群聊 reply_to:
  reply_to 的 msg key 唯一命中 active task。
```

P2P single-active 只进入语义候选；普通 follow-up 必须经 TaskRouter 判断
`attach_task` 或 `new_task`。

其他情况走 TaskRouter，特别是：

- P2P single-active 候选。
- 群聊无 thread 普通消息，仅 sender 在 watch_keys。
- 群聊再次 @ owner。
- 多个 active candidates。
- closed recall candidates。
- 新 sender 加入。
- 候选任务存在但语义可能换题。

消息 normalize、候选检索、TaskRouter route 与 task attach/reopen/new 的关系见 [消息进入与任务归属](./feishu-shadow-agent-flows.md#message-task-routing-flow)。

## 16. Active task tracking

通用 active task tracking，不只跟踪 thread。

默认 watch：

```text
agent/user 回复后 `lifecycle.watch_minutes` 分钟，默认 120 分钟
```

延长：

```text
same_task follow-up:
  延长 `lifecycle.watch_minutes` 分钟

pending approval:
  approval 作为 blocker 保存在 approvals，不改变 task lifecycle；
  默认 `lifecycle.approval_timeout_hours=24` 小时后过期，null 表示永不过期。
```

提前关闭：

- 对方明确“好/已解决/没问题/谢谢”。
- Hermes 输出 close。
- owner reject。
- owner 在原 chat/thread 直接回复并触发 `human_taken_over`。

active watch 拉取：

```text
source:
  从 tasks 表查 active tasks：
    status = watching
    watch_until > now

grouping:
  有 thread_id -> active_watch.thread.<thread_id>
  无 thread_id -> active_watch.chat.<chat_id>

P2P:
  复用 p2p ingest 结果优先，必要时按 P2P chat 补拉。

Group with thread_id:
  拉 thread messages。

Group without thread_id:
  按 chat_id 拉 watch window 消息，再用 watch_keys 过滤。
```

同一个 chat/thread 里多个 active task 要合并拉取一次，再分发给各 task matcher。`tasks` 表决定当前要监听哪些 chat/thread；`checkpoints` 只保存每个 chat/thread watch stream 的游标，不作为发现监听目标的来源。某个 chat/thread 没有 active task 后不再拉取；历史 checkpoint 可保留，后续清理。

## 17. Closed task recall

任务超时/关闭后不继续常驻监听普通群消息。

但新触发事件发生时执行 closed task recall：

- @ owner。
- P2P 新消息。
- reply_to 旧 agent 消息。

检索历史候选：

```text
same chat_id
最近 `lifecycle.closed_recall_days` 天，默认 7 天
sender in old watch_keys 或 mentions owner
task_label / last_user_message / last_agent_reply 等存储摘要轻量匹配
reply_to 命中旧 included/agent reply message_id
```

Hermes TaskRouter 判定：

```text
reopen_task
new_task
ignore
ambiguous
```

closed historical candidate 只能通过 `reopen_task` 恢复，或另开 `new_task`、`ignore`、`ambiguous`；`attach_task` 只用于 active candidate。

## 18. TaskRouter candidate card

candidate card 只包含摘要级短文本，不包含历史原文。

```json
{
  "task_id": "t_xxx",
  "status": "watching",
  "chat_id": "oc_xxx",
  "chat_type": "group",
  "root_message_id": "om_root",
  "watch_until": "...",
  "task_label": "分类服务启动失败，用户反馈截图显示服务启动异常并伴随 500",
  "message_count": 3,
  "matched_by": "thread"
}
```

字段：

- `task_label` 最多约 100 中文字符。
- `message_count` 是轻量计数，不包含历史原文，也不是 closed recall 的文本匹配依据。
- Router candidate card 不暴露 `last_user_message` / `last_agent_reply`；需要更多只读上下文时，通过顶层 `context_access` 在 `query_scope` 内查询。
- active candidate 可带 `matched_by`，表示命中来源；完整 watch keys 和消息关联保留在 SQLite。
- `context_access` 如存在，是与 candidate card 并列的顶层 card，不嵌入候选项。

## 19. ApprovalRequest

人工审核统一抽象为 ApprovalRequest，复用 bot 私聊 `/approve|/reject`。发送回复和工具动作都走同一套 approval queue。

```text
ApprovalRequest
  approval_id
  task_id
  type: send_reply | tool_action
  status: pending | approved | rejected | expired
  preview
  payload_json
  created_at
  expires_at
```

`pending` approval 是 blocker；`approved`、`rejected`、`expired` 都是历史状态。`expired` 不关闭 task，不向 task-session agent 注入合成事件，只在 status/replay/operator 视图中可见。

ID：

```text
task_id: t_<hash>
approval_id: a_<hash>
```

`/approve` 和 `/reject` 主接收 `approval_id`：

```text
/approve a_12ab34
/reject a_12ab34
```

允许 task_id shortcut：

```text
/approve t_8f3a92
```

如果该 task 当前只有一个 pending approval，就自动映射；如果多个 pending approvals，提示使用具体 `a_...`。

MVP 审批命令：

```text
/approve <id>
/reject <id>
/send <task_id> <最终回复>
```

`/send` 直接创建并批准一个 `send_reply` approval，仍走 dry-run、幂等和 user 身份发送。

审批命令只在 owner 与 bot 的 P2P approval inbox 中生效。

MVP 不支持 `/reply <task_id> 补充背景`。

## 20. Approval 通知

审批通知由 bot 身份私聊 owner。

通知包含可复制命令行，使用短 ID：

```text
[需要确认] 分类服务启动失败

来源：示例产品群 / 李四
原因：涉及线上服务状态，未自动回复
建议回复：
建议先看一下分类服务的启动日志...

操作：
/approve a_12ab34
/send t_8f3a92 <你的最终回复>
/reject a_12ab34
```

未来 UI 的主界面也以 ApprovalRequest 为核心模型：

```text
Tasks:
  当前所有任务状态

Approvals:
  待 owner 决策的动作
```

owner 命令解析、task_id shortcut、`/approve`、`/reject` 和 `/send` 的闭环见 [审批与手动发送](./feishu-shadow-agent-flows.md#approval-flow)。

## 21. 配置

MVP 配置源以 `config.yaml` 为主，SQLite 存动态状态。通过 `ConfigService` 封装，未来 UI 可迁移到 SQLite 或双写。

`config.yaml` 可含 open_id、chat_id、群名等非密钥配置，不含任何 token/secret。

不允许放：

- app_secret
- user_access_token
- tenant_access_token
- refresh_token
- keychain material
- OpenAI/Hermes API key

提供 `config.example.yaml`。本地真实配置默认 `.gitignore`。

只支持单 owner：

```yaml
owner:
  open_id: ou_owner
  name: 张三
```

## 22. SQLite schema

MVP 核心表：

```text
messages
tasks
task_messages
task_watch_keys
approvals
actions
dispatch_attempts
resources
checkpoints
runs
health_checks
chat_policies
config_suggestions
```

关键唯一约束：

```text
messages.message_id unique
tasks.short_id unique
approvals.short_id unique
actions.idempotency_key unique
dispatch_attempts.claim_token unique
checkpoints.key primary key
task_watch_keys(task_id, key) unique
task_messages(task_id, message_id) unique
```

`raw_json` 保存飞书消息原始 JSON，默认保留 30 天。标准化字段长期保留。

```yaml
retention:
  raw_message_days: 30
  resource_days: 30
```

资源文件默认保留 30 天；任务未关闭或 approval pending 时不清理。清理后 `resources` 表保留元数据、hash、file_key、download_status。

## 23. 日志与审计

使用 JSONL 结构化日志：

```text
logs/agent.jsonl
```

示例：

```json
{
  "ts": "2026-06-22T10:00:00+08:00",
  "level": "info",
  "run_id": "run_xxx",
  "task_id": "t_xxx",
  "event": "message_ingested",
  "data": {}
}
```

Agent 审计默认保存：

- `backend_provider`
- `agent_session_id`
- `task_id`
- `tool_permissions_profile`
- request type: router/task_session
- input message IDs / resource IDs
- endpoint/model metadata
- response JSON
- error/latency

不默认保存完整 prompt 原文。消息原文已在 `messages.raw_json` 中。调试可开：

```yaml
debug:
  save_full_agent_io: false
```

不单独维护 prompt_version。通过 `runs.git_commit` 和 `runs.git_dirty` 回溯代码与 prompt 版本。

git dirty 允许运行，但 doctor 和 runs 记录并提示。

## 24. CLI 命令

### doctor

```bash
python -m feishu_shadow_agent doctor
python -m feishu_shadow_agent doctor --send-test
```

默认不发真实消息，只 dry-run。`--send-test` 才给 owner 发测试通知。

检查：

- Python 依赖。
- `lark-cli` 路径和版本。
- `auth status --verify`。
- 必需 user scopes。
- bot 身份基础可用。
- owner open_id 已配置。
- approval inbox 可发送 dry-run。
- 数据库可写。
- Hermes CLI `--version` critical check 和 `status` warning check。
- Hermes tool permission profile 到 CLI flag 的派生检查。
- 配置 YAML schema。

### daemon

```bash
python -m feishu_shadow_agent daemon
python -m feishu_shadow_agent daemon --dry-run
python -m feishu_shadow_agent daemon --dry-run --send-owner-notifications
```

`--dry-run`：

- 拉消息、建任务、调用 Hermes 正常。
- 不真实对外发送。
- 不真实执行写操作。
- 不发送 owner 通知。
- 记录 would-do。

`--dry-run --send-owner-notifications`：

- 允许只发给 owner 的通知。
- 不对外回复。
- 不执行写操作。

### replay

```bash
python -m feishu_shadow_agent replay --message-id om_xxx --dry-run
```

MVP 只支持本地已有 `messages.raw_json` 的 message_id。

### status

```bash
python -m feishu_shadow_agent status
```

显示：

- daemon 最近 run 状态。
- pending approvals。
- active tasks。
- paused health reason。
- 最近错误。

### approval fallback

```bash
python -m feishu_shadow_agent approve a_xxx
python -m feishu_shadow_agent reject a_xxx
python -m feishu_shadow_agent send t_xxx "最终回复"
```

作为 bot 私聊审批的本地备用入口。

### config

```bash
python -m feishu_shadow_agent config show --redacted
```

MVP 不做 `config set`。

## 25. Idempotency

短 task id 使用稳定 hash，冲突时加后缀。内部可另存 full UUID。

发送 idempotency key 保持短小：

```text
reply-<short_hash>
```

所有自动回复、`/approve`、`/send` 真正发送前：

```text
prepare_send
  -> create dispatch_attempt with claim_token
  -> lark-cli ... --dry-run
  -> record dry_run result
  -> actual send
  -> record sent_message_id
  -> messages-mget 读回验证 reply_to 和 mentions
```

同一 `task_id + reply_target_message_id` 只允许一个 `pending` / `sending` / `failed_needs_review` 的 `send_reply` action。自动回复、`/approve` 和 `/send` 并发命中同一目标消息时，由 SQLite 唯一约束或事务锁挡住重复发送；失败方转为 no-op 或提示已有 in-flight send。

如果 dry-run 失败或有证据证明真实发送未发生，action 进入 `failed`，可以由本地 `dispatch retry` 保留原 idempotency key 重新入队。真实发送边界之后的超时、异常、缺少 sent_message_id 或 stale `sending` 都进入 `failed_needs_review`；系统只记录证据并等待本地 `dispatch inspect|mark-sent|retry|cancel`，不自动重发。`failed_needs_review` 会继续占用 active-send 约束，直到 operator `mark-sent`、`retry` 复用原 action/idempotency key，或 `cancel` 释放约束。

发送动作的幂等键、dry-run、实际发送和读回验证见 [幂等发送与读回验证](./feishu-shadow-agent-flows.md#idempotent-send-flow)。

## 26. 后续迭代

明确后续但不进 MVP：

- macOS LaunchAgent / Windows service / Ubuntu systemd。
- 本地 Web UI / TUI 审批台。
- 飞书交互卡片。
- 配置 UI 和 `ApprovalRequest(type=config_change)`。
- `/reply` 补充背景。
- `/retry` 重试资源下载或任务处理。
- per-user policy UI。
- SDK 接入和自管 OAuth token。
- 向量检索历史任务。
- 更细资源下载策略和文件类型分析。
