# Codex 直连 AI 同事

`coworker run --runtime codex` 使用项目的飞书接入、队列和机器人发布，Codex App Server 管理会话与原生子 Agent。旧 `daemon` 入口仍使用 SQLite 策略中的 `agent_direct` 或 `project`；Hermes 原生路径见 [Hermes Gateway AI 同事](hermes-coworker.md)。

## 准备和启动

首次联调使用独立飞书应用和测试群。应用需要机器人、消息读取与回复、群信息权限，并以长连接订阅 `im.message.receive_v1`。接收未 @ 的群消息需要应用身份权限 `im:message.group_msg`；权限生效后才能使用无 @ 引用和话题关注。将机器人加入群。配置 `lark-cli` 与长连接使用同一个应用，`FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 通过环境或凭据管理提供。不要同时让另一个接收方连接此应用。

启动前按首次配置的步骤用 owner 账号完成 `lark-cli auth login --scope …` 并核验授权。`doctor` 会检查用户授权，审批收件箱用该身份查找 owner 的命令。项目用机器人身份发布答复。见[创建应用并授权](../getting-started.md#2-创建应用并授权)。

基于 `config.example.yaml` 准备本地 `config.yaml`，填写 `owner.open_id`、独立测试群及工作目录。测试群策略至少包含：

```yaml
storage:
  sqlite_path: /path/to/coworker-state/agent.sqlite3
agent_backend:
  working_dir: /path/to/coworker-workspace
  codex:
    model: gpt-6-luna
chats:
  oc_test_group:
    auto_reply: true
    bot_joined: true
    reply_identity: bot
    allow_user_fallback: false
coworker:
  enabled: true
  chat_id: oc_test_group
  provider: codex
  memory_maintenance: false
  shared_memory_path: /path/to/coworker-workspace/coworker-memory.md
```

首次测试可使用新建的 SQLite 文件。示例选择 `gpt-6-luna`；将 `agent_backend.codex.model` 设为 `null` 可沿用 Codex 自身默认模型。子 Agent 使用 Codex 原生模型配置。更新已有策略时，导入命令须加 `--replace`。旧会话继续使用原工作目录，群记忆路径由策略中的 `coworker.shared_memory_path` 决定。

安装依赖并导入生产策略：

```bash
uv sync --locked --extra cards
uv run --locked python -m feishu_shadow_agent policy import-config --config config.yaml
```

已存在的 dry-run 库不会自动跟随生产策略变化。更新生产策略后，如需覆盖预演策略，执行：

```bash
uv run --locked python -m feishu_shadow_agent policy sync-production --config config.yaml --mode dry-run
```

检查并启动预演：

```bash
uv run --locked python -m feishu_shadow_agent doctor --config config.yaml --mode dry-run
uv run --locked python -m feishu_shadow_agent coworker run --runtime codex --config config.yaml --reply-mode quote --dry-run
uv run --locked python -m feishu_shadow_agent coworker status --config config.yaml --mode dry-run
```

`--reply-mode` 取 `quote`、`topic` 或 `auto`，默认 `quote`。主区短答可引用原消息；`topic` 将主区结果发布到新话题；`auto` 由 Agent 选择。需要持续处理的任务先返回接单内容，项目取得话题回执后，在新 Codex 会话执行。已有话题始终留在原话题。dry-run 只生成飞书 CLI 预览，不能创建真实话题，也不会启动依赖话题回执的后续任务。

dry-run 对 Codex 使用只读、禁网络的 sandbox，关闭 shell、MCP、应用和多 Agent 等工具，并核查 MCP 工具列表；只恢复由此隔离方式创建的 dry-run 会话。

生产会话沿用 Codex 的原生 sandbox、网络、MCP、应用和工具配置。本群话题和历史还可通过项目提供的 `read_feishu_topic`、`read_feishu_group_history` 只读工具访问，返回内容须属于当前授权群。这些工具在新建直连线程时注册；旧 UUID 如果没有这些工具，保留会话时不能按需调用。项目登记自己发布的回复和回执；原生业务工具的副作用受 Codex 自身权限和配置约束。

预演通过并准备真实发送时，先检查生产配置，再启动：

```bash
uv run --locked python -m feishu_shadow_agent doctor --config config.yaml
uv run --locked python -m feishu_shadow_agent coworker run --runtime codex --config config.yaml --reply-mode quote
```

## 消息、关注和记忆

普通群主区共享一个会话，话题各有会话；@ 或引用已确认的机器人回复可触发。owner 的每个未引用私聊请求使用新会话，引用对应请求或机器人回复时续接；转发内容以飞书接口返回的内容为准。项目只回复当前对话，不自动向转发来源发消息。

项目只将当前对话内可读取且未撤回的引用加入提示，并在发布前核对引用快照。owner 私聊的审批命令由审批收件箱处理。手工 `/send` 可回复 owner 的私聊请求，或在已确认的话题中回复接单消息；发布前仍会核对原请求。

任务运行期间引用当前请求的新消息，先由短生命周期的只读 Codex App Server 判断关联性；判断使用不持久化的原生临时线程。明确属于当前任务时送入当前轮；无关、无法判断或分类超时时排入下一轮。发布前先判断同话题已定位且可领取的补充；暂停接手、重试延后或服务停止时保留排队。判断期间当前轮已结束时，相关更正进入下一轮，旧答复不发布。判断模型的执行超时为 30 秒，不调用业务工具，并发数与 `max_concurrent_topics` 一致。

owner 在真实话题中 @ 机器人并明确要求关注或取消关注。关注默认 24 小时，也可在请求中指定期限。项目核对请求及本轮使用的消息快照后更新关注状态。关注期间连续消息合并判断，Agent 只在相关时回复。到期或取消后普通消息不再触发，@ 和引用仍可触发。事件是否送达由飞书应用权限决定。

新建直连会话关闭 Codex 用户级原生记忆自动读写。群记忆使用 `coworker.shared_memory_path`，私聊明确要求记住时使用同路径的 `.private` 文件；dry-run 使用独立后缀。保留旧 UUID 时，Codex 0.167.0 对已有启用记忆的线程不能仅凭恢复参数证明已关闭；需要严格隔离时先核查旧线程或新建会话。两种文件仍属于同一系统用户，提示词限制不是操作系统级的文件权限隔离。后台群记忆整理默认关闭；旧 SQLite 策略保留原开关，开启时在独立原生会话中整理群资料并关闭原生自动记忆。

## 会话接手和异常处理

`coworker status` 显示会话、投递回执、关注和接手状态，不展示答复正文。暂停一个会话后，项目继续接收入队，等待当前轮、子 Agent 和待发结果，确认 App Server 中的线程卸载后显示 `ready` 和原工作目录下的接手命令：

```bash
uv run --locked python -m feishu_shadow_agent coworker pause --config config.yaml --session-id 1
uv run --locked python -m feishu_shadow_agent coworker status --config config.yaml
# 状态为 ready 时，运行 status 显示的 cd ... && codex --no-daemon resume <UUID>
uv run --locked python -m feishu_shadow_agent coworker resume --config config.yaml --session-id 1
```

`--no-daemon` 使交互进程退出时释放会话。操作员退出 Codex 后再执行 `resume`；项目重新加载原 UUID 后处理积压。`needs_review` 表示卸载或恢复结果不明，核对后可用 `coworker pause --session-id 1 --retry` 重新申请释放。

发送结果不确定时不会自动重跑模型或换幂等键。停止 daemon，核对飞书侧实际结果，再记录回执或丢弃：

```bash
uv run --locked python -m feishu_shadow_agent coworker resolve-delivery --config config.yaml --delivery-id 1 --sent-message-id om_verified --daemon-stopped
# 开题回执还须提供 --thread-id omt_verified
uv run --locked python -m feishu_shadow_agent coworker resolve-delivery --config config.yaml --delivery-id 1 --discard --daemon-stopped
```

两个命令二选一。`--daemon-stopped` 是操作员声明；确认消息和话题 ID 属于该次发送后再记录。`coworker discard-event` 用于处置执行结果不明的入站事件。切换新旧运行时时，先处理对方运行时的排队、执行中和待核对工作；启动器发现冲突会拒绝启动，不会丢弃旧工作。

发送前读取暂时失败时，项目在同一投递内有限重试。开题回执待核对时，相关话题消息保留排队。其他已能定位的话题和独立的私聊请求继续处理。切换到另一个 AI 同事群后，新进程只领取新群和 owner 私聊的工作。旧群的队列、待发结果和已批准的待发回复留在原库中。

SQLite v9、v10、v11、v12 会事务升级到 v13。dry-run 可从这些旧版生产库只读提取策略。升级前备份数据，项目会拒绝打开 v8 及其他不支持的版本。完整业务和真实飞书联调状态见 [验证记录](native-runtime-acceptance-2026-11-19.md)。
