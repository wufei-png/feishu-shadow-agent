# 配置参考

本文描述影子助手和 AI 同事使用的 `config.yaml`。配置模型位于 `src/feishu_shadow_agent/config.py`；`schemas/config.schema.json` 由模型生成，可供编辑器和 CI 使用。Hermes Gateway 可选读取 `hermes_gateway_coworker` 中的群聊伙伴配置，其余设置仍由 Hermes profile 管理，见 [Hermes Gateway 配置](operations/hermes-coworker.md)。

`reply_policy`、`chats` 和 `coworker` 是 Product Policy Store 的策略导入来源。运行 `policy import-config` 后，项目运行时从 SQLite 读取生效策略；全局策略未初始化时，`doctor` 和 daemon 的健康检查会失败。

影子助手只查询已配置的群；设置群策略后仅处理启用时刻之后的新消息，正常运行中的重叠轮询和断线恢复仍会保留。删除群策略会暂停该群的未完成任务、重试和待发送动作；重新添加后需要在 Console 或 CLI 中显式恢复。

生产和 dry-run 使用独立的数据库、资源和日志路径。dry-run 初次创建时仅复制生产库中的当前策略，此后独立修改；`policy sync-production --config config.yaml --mode dry-run` 显式覆盖预演策略，并对被移除群的预演任务执行暂停。生产策略未初始化时，可用 `policy import-config --config config.yaml --mode dry-run` 导入 YAML。

影子助手 dry-run 在查询私聊前枚举并排除机器人会话；枚举失败或超时时跳过本轮私聊。枚举单独使用最多约 10 秒的时间预算，不占用群消息采集预算。

常用命令：

```bash
python -m feishu_shadow_agent config show --config config.yaml --redacted
python -m feishu_shadow_agent config validate --config config.yaml
python -m feishu_shadow_agent config schema
python -m feishu_shadow_agent policy import-config --config config.yaml
python -m feishu_shadow_agent policy import-config --config config.yaml --replace
python -m feishu_shadow_agent policy update-global --config config.yaml --p2p-auto-reply false --reason "pause P2P auto replies"
python -m feishu_shadow_agent policy update-chat --config config.yaml --chat-id oc_xxx --auto-reply false --reason "pause chat"
python -m feishu_shadow_agent reply-style refresh --config config.yaml --dry-run
python -m feishu_shadow_agent reply-style refresh --config config.yaml
python -m feishu_shadow_agent console --config config.yaml --host 127.0.0.1 --port 8765
```

`config validate` 只校验 YAML 结构和 Pydantic 语义，不检查 Feishu、agent backend、SQLite 或 CLI 可用性。运行前完整健康检查仍使用 `doctor`。

## 顶层配置

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `owner` | object | 必填 | 单 owner 配置，用于审批通知和本地 operator 命令。 |
| `daemon` | object | 见下表 | daemon 轮询和消息回看设置。 |
| `health` | object | 见下表 | 运行时健康检查间隔和超时。 |
| `storage` | object | 见下表 | 本地 SQLite 和资源下载目录。 |
| `logging` | object | 见下表 | 本地 JSONL 日志路径。 |
| `lark_cli` | object | 见下表 | `lark-cli` 可执行文件和超时。 |
| `interactive_cards` | object | 见下表 | 审批卡片回调及飞书长连接凭据环境变量。影子助手轮询消息；项目 AI 同事使用长连接接收消息。 |
| `agent_backend` | object | 见下表 | Agent backend 选择、上下文隔离策略和 provider 专属参数。 |
| `coworker` | object | 默认关闭 | 项目 AI 同事的策略导入来源；包括 Codex 直连和兼容 `daemon` 路径。 |
| `coworker.send_mode` | `agent_direct`/`project` | `agent_direct` | 兼容 `daemon` 路径的发送方式。Codex 直连使用自己的项目发布路径；该字段仍参与启动策略一致性检查。修改后需导入策略并重启。 |
| `coworker.partner_bots` | map | `{}` | 兼容 `daemon` 路径中按名称配置飞书伙伴的 `open_id` 和可选 `description`。 |
| `hermes_gateway_coworker.groups` | map | `{}` | Hermes Gateway 的群聊伙伴名单，按群 `chat_id` 配置；需要安装集成补丁。 |
| `coworker.memory_maintenance` | bool | `false` | 启用独立后台记忆整理。已导入 SQLite 的设置保持原值。 |
| `coworker.memory_maintenance_time` | `HH:MM` | `00:00` | 每日本机时区的整理时间。错过定时点后，下次启动 AI 同事时合并补跑一次。 |
| `reply_policy` | object | 见下表 | Policy Import Source 中的全局自动回复策略。 |
| `reply_postprocess` | object | 见下表 | 可选的一次性回复表达改写；默认关闭，不改变现有回复路径。 |
| `chats` | map | `{}` | Policy Import Source 中按 Feishu `chat_id` 声明的群级策略，例如 `oc_xxx`。 |
| `tool_permissions` | enum | `read_only` | 影子助手 Agent backend 的工具权限档位：`read_only`、`full_access` 或 `inherit`。项目 AI 同事使用其原生运行时权限设置。 |
| `retention` | object | 见下表 | 本地数据保留时间。 |
| `lifecycle` | object | 见下表 | 全局任务生命周期和审批过期设置。 |
| `debug` | object | 见下表 | 调试用持久化开关。 |

未知字段和同一 YAML 映射内的重复显式键会被拒绝，错误提示包含文件位置；通过 YAML 合并锚点显式覆盖默认值仍然有效。兼容旧配置时，顶层 `hermes` 会在读入时迁移为 `agent_backend.hermes`，`debug.save_full_hermes_io` 会作为 `debug.save_full_agent_io` 的旧别名读入；新配置应使用表中的新字段。真实密钥不要写入 `config.yaml`，只允许写环境变量名，例如 `agent_backend.hermes.api_key_env`。

## 字段明细

| 字段 | 类型/可选值 | 默认值 | 行为影响 |
| --- | --- | --- | --- |
| `owner.open_id` | string | 必填 | owner 的飞书 open_id；缺失时配置校验失败。 |
| `owner.name` | string | `""` | 仅用于人读配置和日志，不作为身份标识。 |
| `daemon.tick_interval_seconds` | int `> 0` | `60` | daemon 两次轮询之间的秒数。 |
| `daemon.overlap_seconds` | int `>= 0` | `120` | 消息拉取时向前回看的窗口，降低 Feishu 延迟导致漏消息的风险。 |
| `health.interval_seconds` | int `> 0` | `300` | 运行时完整健康检查刷新间隔。 |
| `health.retry_interval_seconds` | int `> 0` | `60` | 关键健康失败后重试间隔。 |
| `health.timeout_seconds` | int `> 0` | `10` | 健康探测默认超时。 |
| `storage.sqlite_path` | string | `data/agent.sqlite3` | SQLite 路径；相对路径基于配置文件目录解析。 |
| `storage.resource_dir` | string | `data/resources` | 下载资源目录；必须是安全相对路径，绝对路径和 `..` 会被拒绝。 |
| `storage.max_resource_bytes` | int `>= 1` | `52428800` | 单个下载资源的最大字节数；超限资源会被删除并标记为 `too_large`。 |
| `storage.max_resource_dir_bytes` | int `>= 1` | `2147483648` | `resource_dir` 下下载资源的总字节预算；超限会停止后续下载并标记为 `quota_exceeded`。 |
| `logging.jsonl_path` | string | `logs/agent.jsonl` | JSONL 日志路径；相对路径基于配置文件目录解析。 |
| `logging.level` | `debug`/`info`/`warning`/`error` | `info` | 写入日志 sink 的最低级别。 |
| `logging.console` | bool | `false` | 是否同时把人类可读运行日志写到 stderr；不影响命令结果 stdout。 |
| `logging.text_path` | string/null | `null` | 可选普通文本日志文件路径；相对路径基于配置文件目录解析。 |
| `lark_cli.path` | string/null | `null` | 指定 `lark-cli` 路径；`null` 使用当前 `PATH`。 |
| `lark_cli.timeout_seconds` | int `> 0` | `30` | `lark-cli` 子进程调用超时。 |
| `interactive_cards.enabled` | bool | `false` | 是否启用审批卡片与官方 SDK 的 `card.action.trigger` 长连接。关闭或连接不健康时，owner notification 使用文本兜底。 |
| `interactive_cards.app_id_env` | string | `FEISHU_APP_ID` | 飞书长连接应用 ID 的环境变量名；项目 AI 同事接收消息时也使用它。 |
| `interactive_cards.app_secret_env` | string | `FEISHU_APP_SECRET` | 飞书长连接应用密钥的环境变量名；必须与 `app_id_env` 不同。 |
| `interactive_cards.startup_timeout_seconds` | int `> 0` | `10` | daemon 启动时等待卡片回调长连接变为健康的最长秒数；超时后继续以文本兜底运行。 |
| `agent_backend.provider` | `hermes`/`codex`/`claude_code` | `hermes` | Agent backend provider。被选中的 provider 必须覆盖 task router、task session、reply postprocess 和 owner style refresh。 |
| `agent_backend.max_attempts` | int `>= 1` | `3` | 单次 Agent 调用的最大尝试次数。各权限档位共用；`full_access` 和 `inherit` 重试可能重复 Agent 工具副作用，系统不提供额外的副作用幂等兼容层。 |
| `agent_backend.working_dir` | string/null | `null` | Agent 子进程运行目录；`null` 表示 `config.yaml` 所在目录。相对路径基于配置文件目录解析。新 task 创建时会把解析后的绝对路径固化到 `tasks.agent_working_dir`；后续 follow-up/reopen 继续使用 task 内记录的目录，不随配置漂移。无 task 的 router 使用当前配置解析结果。 |
| `agent_backend.config_scope` | `isolated`/`native` | `isolated` | 是否加载普通用户级配置；不等同于清除 credentials、managed policy 或 auth state。各 provider 映射见“Agent Backend 上下文语义”。 |
| `agent_backend.auto_context` | `disabled`/`enabled` | `disabled` | 是否加载 CLI 自带规则、memory、默认 skill 等隐式上下文。不同 CLI 的实际含义不同，见“Agent Backend 上下文语义”。 |
| `agent_backend.explicit_context.paths` | list[string] | `[]` | 非原生 skill 的绝对路径。仅在新建 Task Session prompt 中以“尚未加载、需要时先读取”方式展示；程序不读取、复制、链接或安装这些路径。路径必须是绝对路径，不能包含反引号或控制字符。Hermes 和 Codex 支持；Claude Code v1 不支持。 |
| `agent_backend.hermes.mode` | `cli`/`http` | `cli` | 只影响 `doctor`/运行时 health 探测方式；**无论取何值，任务处理和 runtime backend readiness 都会检查并使用本机 `hermes chat` CLI**。`http` 会额外探测 Hermes gateway/API server 是否可达。 |
| `agent_backend.hermes.path` | string/null | `null` | `cli` 模式下指定 Hermes 路径；`null` 使用当前 `PATH`。 |
| `agent_backend.hermes.source` | string | `feishu-shadow-agent` | 传给 Hermes 会话和审计数据的来源标记；不能为空。官方建议第三方集成可用 `tool` 以从用户 session 列表中过滤；本项目保留自定义标签便于审计区分。 |
| `agent_backend.hermes.router_max_turns` | int `> 0` | `4` | Hermes 任务路由调用的最大 tool iteration 轮数（`--max-turns`）。 |
| `agent_backend.hermes.session_max_turns` | int `> 0` | `8` | Hermes 单任务会话调用的最大 tool iteration 轮数（`--max-turns`）。 |
| `agent_backend.hermes.model` | string/null | `null` | 可选模型覆盖；`null` 时由 Hermes CLI 决定。`config_scope: isolated` 下不会读取用户全局 Hermes 配置。 |
| `agent_backend.hermes.provider` | string/null | `null` | 可选 provider 覆盖；`null` 时由 Hermes CLI 决定。`config_scope: isolated` 下不会读取用户全局 Hermes 配置。 |
| `agent_backend.hermes.timeout_seconds` | int `> 0`/null | `null` | Hermes Agent 子进程超时；`null` 不设置 wall-clock 截止时间。health/doctor 始终使用独立的 `health.timeout_seconds`。 |
| `agent_backend.hermes.skill_paths` | list[string] | `[]` | Hermes 原生 skill 目录或 `SKILL.md` 路径；路径基于配置文件目录解析并通过 `--skills` 传入 Task Session。`doctor` 要求对应 `SKILL.md` 含有效 `name`。 |
| `agent_backend.hermes.health_url` | string/null | `null` | `agent_backend.hermes.mode: http` 时追加使用；必须以 `http://` 或 `https://` 开头，HTTP 模式下必填。典型值为 `http://127.0.0.1:8642/health`。**不用于 chat/路由/会话调用**。 |
| `agent_backend.hermes.api_key_env` | string/null | `HERMES_API_KEY` | `agent_backend.hermes.mode: http` 时可选 Bearer token 环境变量名。Hermes 官方 API server 常用 `API_SERVER_KEY`；`/health` 端点通常无需认证，此字段主要留给需要鉴权的 health URL。 |
| `agent_backend.codex.path` | string/null | `null` | 指定 Codex CLI 路径；`null` 使用当前 `PATH`。 |
| `agent_backend.codex.model` | string/null | `null` | 可选 Codex model 覆盖；`null` 时由 Codex CLI 决定。示例配置选择 `gpt-6-luna`。 |
| `agent_backend.codex.reasoning_effort` | `minimal`/`low`/`medium`/`high`/`xhigh`/`max`/null | `null` | 可选 Codex 推理强度覆盖。直连每轮通过 `turn/start` 传入 `effort`，`codex exec` 读取 `model_reasoning_effort`。设为 `null` 时由 Codex 原生运行时解析。 |
| `agent_backend.codex.timeout_seconds` | int `> 0`/null | `null` | Codex Agent 子进程超时；`null` 不设置 wall-clock 截止时间。readiness 探测使用 `health.timeout_seconds`。 |
| `agent_backend.codex.skills` | list[string] | `[]` | 已由部署环境安装到 Codex 原生发现范围的技能名称。名称只能使用小写字母、数字和内部连字符，最长 64 字符。新会话首轮中，轻量直连提供用途和路径供按需阅读，`codex exec` 使用原生 `$name` 显式调用。 |
| `agent_backend.claude_code.path` | string/null | `null` | 指定 Claude Code CLI 路径；`null` 使用当前 `PATH`。 |
| `agent_backend.claude_code.model` | string/null | `null` | 可选 Claude Code model 覆盖；`null` 时由 Claude Code CLI 决定。 |
| `agent_backend.claude_code.timeout_seconds` | int `> 0`/null | `null` | Claude Code Agent 子进程超时；`null` 不设置 wall-clock 截止时间。readiness 探测使用 `health.timeout_seconds`。 |
| `reply_policy.p2p_auto_reply` | bool | `true` | 导入 Product Policy Store 后，P2P 私聊在回复 gate 通过时是否允许自动回复。 |
| `reply_policy.unknown_group_auto_reply` | bool | `false` | 保留的策略字段；未显式配置的群不会被采集或交给 Agent，因此该字段不会启用未知群的处理。 |
| `reply_postprocess.enabled` | bool | `false` | 是否对 agent 生成的候选回复做一次性表达改写。关闭时不检查 profile/skill 路径，也不调用 backend postprocess。开启时至少需要启用 `owner_style` 或 `humanizer_zh` 之一。 |
| `reply_postprocess.max_turns` | int `> 0` | `4` | 一次性 reply postprocess 和 owner style refresh 的 Hermes `--max-turns`；Codex 和 Claude Code 没有对应 max-turns flag。 |
| `reply_postprocess.model` | string/null | `null` | postprocess/refresh 的模型覆盖；Hermes 继承 `agent_backend.hermes.model`，Codex 继承 `agent_backend.codex.model`，Claude Code 继承 `agent_backend.claude_code.model`。 |
| `reply_postprocess.provider` | string/null | `null` | Hermes postprocess/refresh 的 provider 覆盖；Codex/Claude Code backend 不使用此字段。 |
| `reply_postprocess.owner_style.enabled` | bool | `false` | 是否让 postprocess 读取 owner style profile 并贴近 owner 的自然回复习惯。 |
| `reply_postprocess.owner_style.profile_path` | string | `data/owner_style.zh.md` | owner style Markdown profile 路径；相对路径基于配置文件目录解析。缺失或不可读时不自动发送，候选回复转 owner review。 |
| `reply_postprocess.owner_style.refresh.lookback_days` | int `>= 1` | `30` | `reply-style refresh` 拉取 owner 回复样本的时间窗口。 |
| `reply_postprocess.owner_style.refresh.max_samples` | int `>= 1` | `300` | refresh 送给 summarizer 的最大过滤后样本数。 |
| `reply_postprocess.owner_style.refresh.min_samples` | int `>= 1` | `20` | refresh 写入 profile 前要求的最小过滤后样本数。 |
| `reply_postprocess.humanizer_zh.enabled` | bool | `false` | 是否让 postprocess 读取 humanizer-zh skill guidance，避免常见 AI 写作痕迹。 |
| `reply_postprocess.humanizer_zh.skill_path` | string/null | `null` | humanizer-zh guidance 文件路径。启用 `humanizer_zh` 时必填；缺失或不可读时不自动发送，候选回复转 owner review。关闭时可保持 `null`。 |
| `chats.<chat_id>.name` | string | `""` | 方便 operator 识别的群名，不作为 chat_id。 |
| `chats.<chat_id>.auto_reply` | bool | `false` | 该群在所有 gate 通过时是否允许自动回复。 |
| `chats.<chat_id>.bot_joined` | bool | `false` | bot 是否已进群；影响 bot 回复和资源访问能力判断。 |
| `chats.<chat_id>.reply_identity` | `bot_preferred`/`bot`/`user` | `bot_preferred` | 群回复身份策略：优先 bot、强制 bot、或 user 代发。 |
| `chats.<chat_id>.allow_user_fallback` | bool | `true` | `bot_preferred` 无法用 bot 时是否允许 fallback 到 user。 |
| `chats.<chat_id>.resource_download` | bool | `true` | 是否允许保存该群消息中的可下载资源。 |
| `retention.raw_message_days` | int `>= 1` | `30` | 非活动任务的消息、任务、审批、动作、Agent 审计和处理错误等敏感内容保留天数；仍处于有效 `watch_until` 的 watching task 暂缓清理。只清空敏感字段，保留最小审计元数据。 |
| `retention.resource_days` | int `>= 1` | `30` | 下载资源文件保留天数。 |
| `retention.feedback_content_days` | int `>= 1` | `30` | 审批反馈中候选回复、最终回复、短备注等敏感文本的保留天数。到期后在原记录中清空并写入 `content_expired_at`。 |
| `lifecycle.watch_minutes` | int `> 0` | `120` | 新消息、follow-up 或 agent 回复后继续监听任务的分钟数。 |
| `lifecycle.burst_attach_seconds` | int `>= 0` | `60` | 同一 chat、同一 sender 的连续触发消息可跳过 TaskRouter 并自动 append 到过滤后唯一 burst-eligible active task 的秒数窗口；`0` 表示关闭。 |
| `lifecycle.closed_recall_days` | int `>= 1` | `7` | 新触发事件可召回 closed task 的天数窗口。 |
| `lifecycle.approval_timeout_hours` | int `>= 1`/null | `24` | pending approval 的过期小时数；`null` 表示永不过期。过期不关闭 task；过期写入只由 daemon tick、审批命令前置处理或显式 maintenance 命令推进。 |
| `debug.save_full_agent_io` | bool | `false` | 是否保存完整 agent 输入输出；常规运行应保持关闭。 |

## 运行时存储安全

当前 `store/schema.sql` 的 SQLite schema 为 v13。空库初始化为 v13，已标记的 v9 至 v12 数据库会事务式升级。项目会拒绝打开其他版本或未标记的已有数据库。预演可从已标记的 v9 至 v13 库只读提取生产策略，其他只读入口以各自说明为准。

每个 SQLite 连接都会启用 `PRAGMA foreign_keys = ON` 和 `PRAGMA busy_timeout = 5000`，避免 daemon、status、replay 或本地审批命令短暂并发时立即报 `database is locked`。

P8 暂不启用 `PRAGMA journal_mode=WAL`。当前部署形态以本地单文件 SQLite、手工复制/回放和简单清理为主；WAL 会额外生成 `-wal`/`-shm` 文件，容易让 operator 复制数据库或清理 `data/` 时漏文件。现阶段先用 busy timeout 改善短暂写锁等待，后续如果需要更高并发再单独评估 WAL。

`storage`、`logging` 和 `storage.resource_dir` 可能位于不同挂载点。`doctor` 与运行时 health 共用同一套只读测量（`runtime_storage_capacity`），检查这些路径所在文件系统的剩余空间：低于 `max(1 GiB, 总量 10%)` 记为 warning，低于 `max(256 MiB, 总量 2%)`（上限 `1 GiB`）记为 critical。critical 会阻断 ingest 与真实发送，并让 daemon 拒绝启动；warning 不阻断任何运行时行为；无法测量（含挂载点返回全 0 或负数这类退化值）只记为 warning，不当作磁盘已满。阈值由代码固定，不可配置。具体用量可在 `status` 的 `storage` 段看到：SQLite、JSONL/文本日志、资源目录用量与配额，以及剩余空间最少的运行时文件系统。

critical 期间 tick 在 health gate 处早退，`retention` 阶段同样不会执行，因此 daemon 不会自动回收空间。恢复需要人工清理磁盘，或运行 `python -m feishu_shadow_agent retention prune --config config.yaml` 删除过期资源后再让 health 重新通过。

`status` 输出中的 `daemon_liveness` 优先反映心跳仍有效的 daemon；没有活跃实例时反映最近一次启动，包括尚未进入首次 tick 的启动。健康详情中的 `last_daemon_start` 保留最近一次启动，即使另一个实例仍在运行，最近的启动失败也会显示为健康问题。`last_run` 仍保留最新任意 run，包括 doctor 等非 daemon run。启动检查进行中显示 `starting`，首次 tick 前启动失败显示 `startup_failed`；运行中的 heartbeat 超过固定阈值未更新时显示 `stale`。`stale` 表示上一次 daemon 可能卡住或崩溃，需要结合 JSONL 日志和进程状态确认。`runs.last_tick_summary_json` 只保留最近一个 tick 的阶段摘要，详细历史仍以 JSONL 为准。

Product Policy Store 是运行时 Product Policy 真相来源，包含全局 `product_policies`、复用的 per-chat `chat_policies` 和 `policy_audits`。默认导入只填缺失的全局策略和缺失的 chat policy；如果 `config.yaml` 省略 `reply_policy`，导入会使用 Pydantic 默认值并在结果中报告 `used_defaults: true`。`--replace` 会替换全局策略和 config 中列出的 chat policy，但不会删除 DB 中存在而 config 缺失的 chat policy。每个插入或替换都会写入 `policy_audits`。这类比较和导入语义叫 Policy Import Source / Policy Import Diff，不叫 config drift。

`policy import-config`、`policy update-global` 和 `policy update-chat` 都通过 OperatorCommandService 返回统一命令结果，并把真实 actor、reason、old/new policy 写入 `policy_audits`。直接 update 命令只修改 Product Policy Store，不写 `config.yaml`。合法变更通过 schema 校验后直接写入并审计；后端不对 auto-reply、resource download、bot joined、reply identity 或 user fallback 等配置变更做风险分级或二次确认。未来 UI 应通过配置项说明和 hover/help 文案解释字段含义，而不是依赖后端风险标签限制 owner 应用合法配置。

`status`、`replay` 和 `dispatch inspect` 是 operator 读路径，不会把 overdue approval 写成 `expired`。超过 `expires_at` 但尚未被显式推进的 approval 仍是 `status: pending`，读模型额外显示 `is_overdue`、`overdue_seconds` 和 `recommended_action`。需要立即推进过期时运行：

```bash
python -m feishu_shadow_agent maintenance expire-approvals --config config.yaml
```

`approve`、`reject`、`send`、`task close`、`task reopen`、`dispatch inspect`、`dispatch mark-sent`、`dispatch retry`、`dispatch cancel`、`maintenance expire-approvals` 和 policy mutation 命令都通过 OperatorCommandService 返回同一类 YAML 命令结果。`changed` 表示目标 operator 动作是否实际推进，具体业务结果在 `result` 中；`dispatch inspect` 成功时是 `status: no_change`，因为它只读取恢复证据。

## Operator Console

本地 Operator Console 通过 Python local console server 提供 bundled Vite/React renderer 和 `/api/*` routes。默认命令：

```bash
python -m feishu_shadow_agent console --config config.yaml --host 127.0.0.1 --port 8765
```

启动时会生成一次性的 bearer token，并在 stdout 输出把 token 放在 URL fragment 的本地访问地址。fragment 不会随 HTTP 请求发送；renderer 会把 token 保存到当前 tab 的 `sessionStorage`，立即从可见 URL 中移除。API 默认只接受 loopback Host header 和 `Authorization: Bearer <token>`，所有响应带 no-store、no-referrer、CSP、nosniff 和禁止嵌入等安全头。

Console 覆盖 Dashboard、Approvals、Tasks、Dispatch、Feedback、Policy、Settings 和 Health。读路径通过 `OperatorQueryService` 暴露 dashboard、queue/detail、反馈统计与差异、Policy/Settings、Message Detail 和 Health DTO；写路径通过 `OperatorCommandService` 执行 approval、dispatch recovery、maintenance expiry 和 Product Policy 命令。它不写 `config.yaml`，不直接读 SQLite，不生成 dispatch preview，也不绕过 Product Policy / OperatorCommandService 边界。Health 展示规范化 issue、runtime liveness 和失败命令/dispatch 摘要，不作为默认 raw log viewer。

资源下载先通过 chat policy，再受本地限额保护。单文件超过 `storage.max_resource_bytes` 时，刚下载的文件会被删除，`resources.download_status` 置为 `too_large`，`path` 置空，并把尝试路径和大小写入 `raw_json`。`resource_dir` 用量超过 `storage.max_resource_dir_bytes` 时，刚下载文件会被删除，当前和后续资源标记为 `quota_exceeded` 且 `path` 置空。`too_large` / `quota_exceeded` 会阻塞 task session agent，并创建 owner notification；第一版不让 agent 在缺少资源的情况下语义降级回答。

影子模式的 Agent backend 默认 `timeout_seconds: null`，不会在固定 60 秒截断；该模式按消息串行执行，一次无限时调用未返回前，本 tick 的后续消息和 dispatch 会暂停。需要故障隔离上限的部署可显式配置正整数。AI 同事模式使用独立的超时和话题并发设置，见[运行说明](operations/single-group-ai-coworker.md)。startup/runtime health 与 doctor 仍由 `health.timeout_seconds` 限时。

## AI 同事的配置作用域

`daemon` 启动的兼容路径使用 `coworker.send_mode` 选择模型直发或项目代发，并使用 `trigger_mode`、`history_source` 和 `partner_bots` 配置触发、历史与伙伴协作。具体行为见[兼容路径手册](operations/single-group-ai-coworker.md)。

`coworker run --runtime codex` 要求 SQLite 生效策略启用 `coworker` 且 `provider: codex`。该路径由项目发布回复，主区回复位置由 `--reply-mode` 控制；关注由 owner 在话题中明确开启，不由兼容路径的 `trigger_mode` 开启。`send_mode` 仍参与启动策略一致性检查，但不选择直连路径的发布方式。生产会话继承 Codex 原生权限，dry-run 使用隔离工具配置；详见 [Codex 直连手册](operations/codex-native-coworker.md)。

`coworker run --runtime hermes` 启动 Hermes 自己的 `coworker` profile，不使用项目 `coworker` 策略或项目数据库。[Hermes Gateway 手册](operations/hermes-coworker.md)列出了该 profile 的配置。

以下字段的默认值来自项目配置模型，导入后的生效值以 SQLite 为准：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `coworker.enabled` | `false` | 启用项目 AI 同事；启用时需要配置群。 |
| `coworker.chat_id` | `null` | 项目 AI 同事接收事件的单个群 ID。 |
| `coworker.provider` | `codex` | 项目 AI 同事的后端；兼容路径可选 `codex` 或 `hermes`，Codex 直连要求 `codex`。 |
| `coworker.max_concurrent_topics` | `3` | Codex 同事可并行处理的主会话数。 |
| `coworker.agent_timeout_seconds` | `600` | 同事单轮 Agent 调用的超时秒数。 |
| `coworker.subagent_timeout_seconds` | `3600` | 正常关闭时等待后台子 Agent 的最长秒数。 |
| `coworker.shared_memory_path` | `data/coworker-shared-memory.md` | 项目同事群记忆文件；相对配置文件目录解析。Codex 直连私聊笔记使用独立后缀。 |
| `coworker.trigger_mode` | `mention` | 兼容路径的触发方式，可选 `mention` 或 `followups`。 |
| `coworker.history_source` | `live_on_demand` | 兼容路径的历史来源，可选 `live_on_demand` 或 `local_cache`。 |
| `coworker.history_limit` | `30` | 兼容路径的本地缓存查询和补拉默认页大小，范围为 1 至 50。 |

## Agent Backend 上下文语义

`config_scope`、`auto_context` 和 `explicit_context` 是 provider-neutral 语义，不应简单套用同名 CLI flag。当前 provider 按下表落地：

| Provider | `config_scope: isolated` | `auto_context: disabled` | `explicit_context` |
| --- | --- | --- | --- |
| Hermes | 传 `--ignore-user-config`。 | 传 `--ignore-rules`，跳过 Hermes 自动规则、memory 和预加载 skill。 | `hermes.skill_paths` 通过原生 `--skills <path>` 传给 Task Session；`explicit_context.paths` 只在新建 session 的 prompt 中标为尚未加载。Router 不接收二者。Hermes resume 可再次携带 `--skills`，但不重复 prompt 路径块。 |
| Codex | 传 `codex exec --ignore-user-config`，但它只是不加载 `$CODEX_HOME/config.toml`；auth 仍使用 `CODEX_HOME`。 | 传 `--ignore-rules`，但该 flag 只跳过 user/project execpolicy `.rules`；禁用 `AGENTS.md`、memory、skills、MCP 等需要更强 wrapper 层隔离。 | 仅在新建 Task Session 的 stdin 末尾追加 `codex.skills` 对应的 `$name`；`explicit_context.paths` 先以尚未加载的路径块展示。程序不使用 `skills.config`，也不判断 Codex 是否实际发现。resume、Router、judge 和 postprocess 不重复加入。 |
| Claude Code | 传 `--setting-sources local`，避免加载 user/project settings；auth 和 managed policy 仍由 Claude Code 自身处理。 | 传 `--safe-mode`，禁用 CLAUDE.md、skills、plugins、hooks、MCP servers、custom commands/agents 等自定义上下文；不使用 `--bare` 作为默认 baseline。 | Claude Code v1 不支持显式传入 native skill 或 `explicit_context.paths`；除 `inherit` 与 `config_scope: native` 组合外，传 `--strict-mcp-config` 和空 MCP config。 |

最重要的边界：Codex `--ignore-rules` 不是“禁项目规则、memory、skills”的总开关；它只处理 execpolicy `.rules`。如果 `agent_backend.provider: codex` 指向含项目规则的工作目录，这些规则仍可能影响 Codex 行为。

## 权限档位

`tool_permissions` 控制传给 agent backend 的工具权限。本项目内置的发送路径还会执行 reply policy、审批队列和 dispatch gate；Agent 自行调用的外部工具不经过这些内置路径。

| `tool_permissions` | Hermes CLI 参数 | Codex CLI 参数 | Claude Code CLI 参数 | 实际边界 |
| --- | --- | --- | --- | --- |
| `read_only` | `--toolsets safe` | top-level `--search --ask-for-approval never` + `exec --sandbox read-only` | `--permission-mode dontAsk --tools Read,Grep,Glob,LS,WebFetch,WebSearch --allowedTools Read,Grep,Glob,LS,WebFetch,WebSearch` | Hermes `safe` 禁用本地写操作类工具；Codex 在只读 sandbox 下运行并禁止交互审批，仍允许 live search；Claude Code 只暴露 read/search 类工具且不允许 Bash。它们都不是“零副作用”。 |
| `full_access` | `--toolsets hermes-cli --yolo` | top-level `--search` + `exec --dangerously-bypass-approvals-and-sandbox` | `--permission-mode bypassPermissions --dangerously-skip-permissions --tools default` | 显式危险模式。Backend 可执行本地写工具，也可能自行调用飞书 CLI 产生外部写入；这些调用不经过项目内置的 policy、approval、dry-run 和 dispatch gate。 |
| `inherit` | 不传 `--toolsets` 或 `--yolo` | 不传 sandbox、approval 或 search 覆盖 | 不传 permission mode 或 tools 覆盖 | 使用选定环境的原生权限，具体生效设置及无 TTY 行为由 backend 决定。与 `config_scope: native` 配合时读取用户原生配置；外部工具副作用不受本项目发送门禁保证。 |

`context_access` 不跟随写权限开放。只要本地 DB 存在，Router 会收到受 `query_scope` 限制的 bounded snapshot 和 read-only SQLite URI；Task Session 已由 Messages 提供会话正文，只收到 read-only URI、allowed tables 与当前 task scope，不再复制 snapshot。Hermes `safe` 没有本地 file/SQLite 工具：Router 使用 snapshot，Task Session 使用 Messages；具备只读 SQLite client 的 backend 才能直接查询 `read_only_uri`。

这里的 Context Access read-only 是逻辑信任边界，不是物理沙箱：在 `full_access` 下，项目依赖模型遵循 prompt 中关于 URI、allowed tables 和 query scope 的约束；backend 仍可能拥有其他本地写工具。需要更严格限制时使用 `read_only` 或外部 sandbox。项目内置飞书发送受代码 gate 保护，Agent 自行调用的 shell/CLI 不受该 gate 约束。

### 非交互子进程下的重要语义

daemon 通过 `subprocess` 以无 TTY 方式调用 `hermes chat -q -Q`。在此模式下，Hermes **不会**弹出交互式危险命令审批；对 `terminal()` 等路径，非 gateway 场景下通常会 **自动放行**（见 Hermes `tools/approval.py`）。

因此：

- `full_access` 是显式危险模式，会启用 Hermes 完整 CLI 工具集（file、terminal、browser、skills、memory 等）并传 `--yolo`；Codex 会传 `--dangerously-bypass-approvals-and-sandbox`；Claude Code 会传 `--permission-mode bypassPermissions --dangerously-skip-permissions --tools default`。
- 项目内置飞书发送路径由结构化 JSON schema、风险 gate、owner 审批、`dry-run`、幂等和 dispatch 策略保护；这些不限制 Agent 自行运行外部命令。
- 若需要更严格的本地副作用控制，应使用 `read_only`。

## Hermes 集成说明

### 调用方式

任务路由和任务会话统一构造：

```text
hermes chat -q <prompt> -Q --source <source> --toolsets <...> [--yolo] --max-turns N [--ignore-user-config] [--ignore-rules] [--skills <path> ...] [--resume <session_id>] [--model ...] [--provider ...]
```

- `-Q`：程序化模式，stdout 为模型最终回复，stderr 输出 `session_id:`。
- `--ignore-user-config`：默认由 `agent_backend.config_scope: isolated` 启用，避免用户全局配置改变后台服务行为。
- `--ignore-rules`：默认由 `agent_backend.auto_context: disabled` 启用，跳过 `AGENTS.md`、`SOUL.md`、memory 和预加载 skill 的自动注入，避免仓库/编辑器规则污染飞书任务 prompt。
- `--skills <path>`：只在 task session 中根据 `agent_backend.hermes.skill_paths` 注入；task router 默认不请求 skill，保持路由决策更窄、更稳定。

轻量直连在生产新会话首轮使用 App Server 的 `skills/list` 发现已启用的配置技能，把用途和路径放在本轮规则和当前请求之间。Codex 按请求相关性读取对应的 `SKILL.md`。`codex exec` 在新建 Task Session 时使用 `$name` 显式调用。部署环境负责安装技能。恢复旧会话时沿用原线程上下文。

影子模式 Codex Task Session 通常通过 CLI `-c developer_instructions=...` 传入固定的非交互约束：工具探索保持静默，只返回一个最终 JSON 对象。`tool_permissions: inherit` 且 `config_scope: native` 时不传该覆盖项，以保留用户原有的 `developer_instructions`。结构化输出使用 `--output-schema`、`--output-last-message` 和 Python schema 校验。旧 `daemon` 的 AI 同事直发模式使用自然输出；`coworker run --runtime codex` 使用独立的项目发布路径，见 [Codex 直连运行说明](operations/codex-native-coworker.md)。

影子模式的生产 Task Session 输入使用紧凑的 Markdown 混合格式，固定顺序为 Instructions、Reply Context、Messages、非空 Resources、可选 Context Access、Output Contract，Codex 新 session 的显式 skill 引用随后占最后一行。Messages 是飞书正文的唯一嵌入来源；Reply Context 只保留 current/root id、合法回复目标和非空 chat type；Context Access 不复制消息 snapshot；空 Resources section 和 null message 字段完全省略。Output Contract 是模型可见的精简字段约束，最终 JSON 仍由 provider-native schema（支持时）和 Python schema 双重校验。Router、reply postprocess 和 owner style refresh 保持各自现有输入格式。

Reply postprocess 和 owner style refresh 也是 `hermes chat -q -Q` 一次性调用，但固定使用 read-only tool policy：

```text
--toolsets safe --max-turns reply_postprocess.max_turns
```

它们不传 `--resume`，不注入 native skill 请求或 `agent_backend.explicit_context.paths`，也不改变 task session。`reply_postprocess.model/provider` 为 `null` 时继承所选 backend 支持的 model/provider 配置。

`reply-style refresh --dry-run` 只拉取和过滤 owner 样本，输出计数和基础字符统计，不调用 Agent、不写 profile。群样本仅来自已配置群；私聊样本在完整枚举并排除机器人会话后按 chat ID 查询。枚举失败时只跳过私聊样本，并在统计中返回 `p2p_scope_error`。非 dry-run 在样本数达到 `min_samples` 后调用 summarizer，成功后通过临时文件原子替换 `owner_style.profile_path`。

### `mode: http` 的范围

`agent_backend.hermes.mode: http` 会在 CLI backend readiness 检查之外，对 `agent_backend.hermes.health_url` 追加 `GET`，可选带 `agent_backend.hermes.api_key_env` 中的 Bearer token。任务处理、路由、会话恢复**始终**走 CLI，与 `mode` 无关。

## Codex 集成说明

### 调用方式

Codex backend 使用 provider-native structured output：

```text
codex --search --ask-for-approval never exec --sandbox read-only --json --output-schema <schema.json> --output-last-message <out.json> -
codex --search --ask-for-approval never exec --sandbox read-only --json --output-schema <schema.json> --output-last-message <out.json> resume <session_id> -
```

`full_access` 会把 `--sandbox read-only` 替换为 `--dangerously-bypass-approvals-and-sandbox`。当 `agent_backend.codex.model` 或 `reply_postprocess.model` 配置后，会传 `--model <model>`；`agent_backend.codex.reasoning_effort` 会通过 `-c model_reasoning_effort=<effort>` 应用于 Codex 调用。新 Task Session 只在 stdin 最后一行追加配置的 `$name`；不会生成 `skills.config`，后续 resume 不重复注入。

- prompt 通过 stdin 传入，不放进 argv。
- `--output-schema` 使用当前 Pydantic 输出模型生成的临时 JSON Schema。
- `--output-last-message` 是最终 JSON 输出来源；stdout JSONL 中的 `thread.started.thread_id` 会作为 agent session id 持久化。
- task session follow-up 使用 `codex exec resume <session_id> -`。如果配置切换 provider，已有非 Codex session id 不会被 Codex 复用。
- `doctor` 的 selected-backend readiness 对 Codex 检查 `--version`、`login status`、`exec --help` 和 `exec resume --help`，不运行完整 `codex doctor`，避免把安装更新、terminal 状态或历史线程扫描等无关项变成 daemon 阻塞条件。
- `doctor` 只把 `agent_backend.codex.skills` 报告为 `configured` 并依赖上述 backend readiness；没有稳定公共接口时，不把名称伪报为 loaded/available。

## Claude Code 集成说明

### 调用方式

Claude Code backend 使用 provider-native structured output：

```text
claude -p --output-format json --json-schema <schema-json> --permission-mode dontAsk --tools Read,Grep,Glob,LS,WebFetch,WebSearch --allowedTools Read,Grep,Glob,LS,WebFetch,WebSearch --mcp-config '{"mcpServers":{}}' --strict-mcp-config [--setting-sources local] [--safe-mode] [--add-dir <cwd>] [--model ...]
claude -p --output-format json --json-schema <schema-json> ... --resume <session_id>
```

`full_access` 会把 read-only 工具 allowlist 替换为 `--permission-mode bypassPermissions --dangerously-skip-permissions --tools default`。Reply postprocess 和 owner style refresh 固定使用 read-only policy，即使主 task session 配置为 `full_access`。

- prompt 通过 stdin 传入，不放进 argv。
- `--json-schema` 使用当前 Pydantic 输出模型生成的内联 JSON Schema。
- `--output-format json` 的 `structured_output` 是优先解析来源；`session_id` 会作为 agent session id 持久化。
- task session follow-up 使用 `claude -p ... --resume <session_id>`。如果配置切换 provider，已有非 Claude Code session id 不会被 Claude Code 复用。
- `doctor` 的 selected-backend readiness 对 Claude Code 检查 `--version`、`auth status` 和 `-p --help`，不运行完整模型调用或 `claude doctor`，避免把升级器、workspace trust、MCP 启动或模型费用变成 daemon 阻塞条件。

## Schema

生成并查看 JSON Schema：

```bash
python -m feishu_shadow_agent config schema
```

仓库内提交的 `schemas/config.schema.json` 必须与当前 Pydantic 模型生成结果一致。修改配置模型后，应重新生成该文件并运行配置测试。
