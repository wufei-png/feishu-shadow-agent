# 配置参考

Feishu Shadow Agent 的 `config.yaml` 结构真相来源是 `src/feishu_shadow_agent/config.py` 中的 Pydantic 模型。`config.yaml` 启动时由 `ConfigService.load()` 校验；`schemas/config.schema.json` 是从同一模型生成的 JSON Schema，主要给编辑器、CI 和文档引用使用。

`reply_policy` 和 `chats` 是 Product Policy Store 的 Policy Import Source：operator 必须显式运行 `policy import-config` 才会把这些字段写入 SQLite。daemon、资源下载 preflight 和回复 gate 的运行时 Product Policy 只从 SQLite Product Policy Store 读取；DB 全局策略未初始化时，`doctor` 和 daemon runtime health 会 critical fail closed。

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
| `agent_backend` | object | 见下表 | Agent backend 选择、上下文隔离策略和 provider 专属参数。 |
| `reply_policy` | object | 见下表 | Policy Import Source 中的全局自动回复策略。 |
| `reply_postprocess` | object | 见下表 | 可选的一次性回复表达改写；默认关闭，不改变现有回复路径。 |
| `chats` | map | `{}` | Policy Import Source 中按 Feishu `chat_id` 声明的群级策略，例如 `oc_xxx`。 |
| `tool_permissions` | enum | `read_only` | Agent backend 工具权限档位：`read_only` 或 `full_access`。Hermes、Codex 和 Claude Code backend 都会映射到各自 CLI 的权限边界。 |
| `retention` | object | 见下表 | 本地数据保留时间。 |
| `lifecycle` | object | 见下表 | 全局任务生命周期和审批过期设置。 |
| `debug` | object | 见下表 | 调试用持久化开关。 |

未知字段会被拒绝。兼容旧配置时，顶层 `hermes` 会在读入时迁移为 `agent_backend.hermes`，`debug.save_full_hermes_io` 会作为 `debug.save_full_agent_io` 的旧别名读入；新配置应使用表中的新字段。真实密钥不要写入 `config.yaml`，只允许写环境变量名，例如 `agent_backend.hermes.api_key_env`。

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
| `agent_backend.provider` | `hermes`/`codex`/`claude_code` | `hermes` | Agent backend provider。被选中的 provider 必须覆盖 task router、task session、reply postprocess 和 owner style refresh。 |
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
| `agent_backend.codex.model` | string/null | `null` | 可选 Codex model 覆盖；`null` 时由 Codex CLI 决定。 |
| `agent_backend.codex.reasoning_effort` | `minimal`/`low`/`medium`/`high`/`xhigh`/null | `null` | 可选 Codex reasoning effort 覆盖；配置后通过 `model_reasoning_effort` 传给 Codex CLI。Hermes 不读取此字段。 |
| `agent_backend.codex.timeout_seconds` | int `> 0`/null | `null` | Codex Agent 子进程超时；`null` 不设置 wall-clock 截止时间。readiness 探测使用 `health.timeout_seconds`。 |
| `agent_backend.codex.skills` | list[string] | `[]` | 已由部署环境安装到 Codex 原生发现范围的 skill 名称。名称只能使用小写字母、数字和内部连字符，最长 64 字符；新建 Task Session prompt 最后一行追加 `$name`。配置表示 requested/configured，不代表 runtime loaded。 |
| `agent_backend.claude_code.path` | string/null | `null` | 指定 Claude Code CLI 路径；`null` 使用当前 `PATH`。 |
| `agent_backend.claude_code.model` | string/null | `null` | 可选 Claude Code model 覆盖；`null` 时由 Claude Code CLI 决定。 |
| `agent_backend.claude_code.timeout_seconds` | int `> 0`/null | `null` | Claude Code Agent 子进程超时；`null` 不设置 wall-clock 截止时间。readiness 探测使用 `health.timeout_seconds`。 |
| `reply_policy.p2p_auto_reply` | bool | `true` | 导入 Product Policy Store 后，P2P 私聊在回复 gate 通过时是否允许自动回复。 |
| `reply_policy.unknown_group_auto_reply` | bool | `false` | 导入 Product Policy Store 后，未显式配置的群是否允许自动回复；不影响资源下载、bot 是否入群或 user fallback。 |
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
| `reply_postprocess.humanizer_zh.skill_path` | string | `/Users/wufei2/.agents/skills/humanizer-zh/SKILL.md` | humanizer-zh guidance 文件路径。缺失或不可读时不自动发送，候选回复转 owner review。 |
| `chats.<chat_id>.name` | string | `""` | 方便 operator 识别的群名，不作为 chat_id。 |
| `chats.<chat_id>.auto_reply` | bool | `false` | 该群在所有 gate 通过时是否允许自动回复。 |
| `chats.<chat_id>.bot_joined` | bool | `false` | bot 是否已进群；影响 bot 回复和资源访问能力判断。 |
| `chats.<chat_id>.reply_identity` | `bot_preferred`/`bot`/`user` | `bot_preferred` | 群回复身份策略：优先 bot、强制 bot、或 user 代发。 |
| `chats.<chat_id>.allow_user_fallback` | bool | `true` | `bot_preferred` 无法用 bot 时是否允许 fallback 到 user。 |
| `chats.<chat_id>.resource_download` | bool | `true` | 是否允许保存该群消息中的可下载资源。 |
| `retention.raw_message_days` | int `>= 1` | `30` | 原始消息 payload 保留天数。 |
| `retention.resource_days` | int `>= 1` | `30` | 下载资源文件保留天数。 |
| `lifecycle.watch_minutes` | int `> 0` | `120` | 新消息、follow-up 或 agent 回复后继续监听任务的分钟数。 |
| `lifecycle.burst_attach_seconds` | int `>= 0` | `60` | 同一 chat、同一 sender 的连续触发消息可跳过 TaskRouter 并自动 append 到过滤后唯一 burst-eligible active task 的秒数窗口；`0` 表示关闭。 |
| `lifecycle.closed_recall_days` | int `>= 1` | `7` | 新触发事件可召回 closed task 的天数窗口。 |
| `lifecycle.approval_timeout_hours` | int `>= 1`/null | `24` | pending approval 的过期小时数；`null` 表示永不过期。过期不关闭 task；过期写入只由 daemon tick、审批命令前置处理或显式 maintenance 命令推进。 |
| `debug.save_full_agent_io` | bool | `false` | 是否保存完整 agent 输入输出；常规运行应保持关闭。 |

## 运行时存储安全

每个 SQLite 连接都会启用 `PRAGMA foreign_keys = ON` 和 `PRAGMA busy_timeout = 5000`，避免 daemon、status、replay 或本地审批命令短暂并发时立即报 `database is locked`。

P8 暂不启用 `PRAGMA journal_mode=WAL`。当前部署形态以本地单文件 SQLite、手工复制/回放和简单清理为主；WAL 会额外生成 `-wal`/`-shm` 文件，容易让 operator 复制数据库或清理 `data/` 时漏文件。现阶段先用 busy timeout 改善短暂写锁等待，后续如果需要更高并发再单独评估 WAL。

`status` 输出中的 `daemon_liveness` 来自最新有 daemon tick 的 run（例如 `last_tick_started_at IS NOT NULL`）的 heartbeat；`last_run` 仍保留最新任意 run，包括 doctor 等非 daemon run。该 daemon run 仍是 `running` 且 `last_heartbeat_at` 超过固定阈值未更新时，会显示为 `stale`；这表示上一次 daemon 可能卡住或崩溃，需要结合 JSONL 日志和进程状态确认。`runs.last_tick_summary_json` 只保留最近一个 tick 的阶段摘要，详细历史仍以 JSONL 为准。

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

启动时会生成一次性的 bearer token，并在 stdout 输出带 `token` 的本地访问 URL。API 默认只接受 loopback Host header 和 `Authorization: Bearer <token>`；renderer 会把 URL token 保存到当前浏览器 session 并从可见 URL 中移除。

Console 覆盖 Dashboard、Approvals、Tasks、Dispatch、Policy、Settings 和 Health。读路径通过 `OperatorQueryService` 暴露 dashboard、queue/detail、Policy/Settings、Message Detail 和 Health DTO；写路径通过 `OperatorCommandService` 执行 approval、dispatch recovery、maintenance expiry 和 Product Policy 命令。它不写 `config.yaml`，不直接读 SQLite，不生成 dispatch preview，也不绕过 Product Policy / OperatorCommandService 边界。Health 展示规范化 issue、runtime liveness 和失败命令/dispatch 摘要，不作为默认 raw log viewer。

资源下载先通过 chat policy，再受本地限额保护。单文件超过 `storage.max_resource_bytes` 时，刚下载的文件会被删除，`resources.download_status` 置为 `too_large`，`path` 置空，并把尝试路径和大小写入 `raw_json`。`resource_dir` 用量超过 `storage.max_resource_dir_bytes` 时，刚下载文件会被删除，当前和后续资源标记为 `quota_exceeded` 且 `path` 置空。`too_large` / `quota_exceeded` 会阻塞 task session agent，并创建 owner notification；第一版不让 agent 在缺少资源的情况下语义降级回答。

Agent backend 的默认 `timeout_seconds: null` 允许模型完成配置的 turns 和工具探索，不会在固定 60 秒截断。daemon 当前按消息串行执行 Agent；一次无限时调用未返回前，本 tick 的后续消息和 dispatch 会暂停。需要故障隔离上限的部署可显式配置正整数。startup/runtime health 与 doctor 不继承该值，仍由 `health.timeout_seconds` 限时。

## Agent Backend 上下文语义

`config_scope`、`auto_context` 和 `explicit_context` 是 provider-neutral 语义，不应简单套用同名 CLI flag。当前 provider 按下表落地：

| Provider | `config_scope: isolated` | `auto_context: disabled` | `explicit_context` |
| --- | --- | --- | --- |
| Hermes | 传 `--ignore-user-config`。 | 传 `--ignore-rules`，跳过 Hermes 自动规则、memory 和预加载 skill。 | `hermes.skill_paths` 通过原生 `--skills <path>` 传给 Task Session；`explicit_context.paths` 只在新建 session 的 prompt 中标为尚未加载。Router 不接收二者。Hermes resume 可再次携带 `--skills`，但不重复 prompt 路径块。 |
| Codex | 传 `codex exec --ignore-user-config`，但它只是不加载 `$CODEX_HOME/config.toml`；auth 仍使用 `CODEX_HOME`。 | 传 `--ignore-rules`，但该 flag 只跳过 user/project execpolicy `.rules`；禁用 `AGENTS.md`、memory、skills、MCP 等需要更强 wrapper 层隔离。 | 仅在新建 Task Session 的 stdin 末尾追加 `codex.skills` 对应的 `$name`；`explicit_context.paths` 先以尚未加载的路径块展示。程序不使用 `skills.config`，也不判断 Codex 是否实际发现。resume、Router、judge 和 postprocess 不重复加入。 |
| Claude Code | 传 `--setting-sources local`，避免加载 user/project settings；auth 和 managed policy 仍由 Claude Code 自身处理。 | 传 `--safe-mode`，禁用 CLAUDE.md、skills、plugins、hooks、MCP servers、custom commands/agents 等自定义上下文；不使用 `--bare` 作为默认 baseline。 | Claude Code v1 不支持 native skill 或 `explicit_context.paths`；每次调用都传 `--strict-mcp-config` 和空 MCP config，避免加载全局 MCP。 |

最重要的边界：Codex `--ignore-rules` 不是“禁项目规则、memory、skills”的总开关；它只处理 execpolicy `.rules`。如果 `agent_backend.provider: codex` 指向含项目规则的工作目录，这些规则仍可能影响 Codex 行为。

## 权限档位

`tool_permissions` 控制传给 agent backend 的工具权限。Backend CLI 权限之外，本项目仍会叠加 reply policy、审批队列和 dispatch gate。它与飞书对外发送权限是两套机制。

| `tool_permissions` | Hermes CLI 参数 | Codex CLI 参数 | Claude Code CLI 参数 | 实际边界 |
| --- | --- | --- | --- | --- |
| `read_only` | `--toolsets safe` | top-level `--search --ask-for-approval never` + `exec --sandbox read-only` | `--permission-mode dontAsk --tools Read,Grep,Glob,LS,WebFetch,WebSearch --allowedTools Read,Grep,Glob,LS,WebFetch,WebSearch` | Hermes `safe` 禁用本地写操作类工具；Codex 在只读 sandbox 下运行并禁止交互审批，仍允许 live search；Claude Code 只暴露 read/search 类工具且不允许 Bash。它们都不是“零副作用”。 |
| `full_access` | `--toolsets hermes-cli --yolo` | top-level `--search` + `exec --dangerously-bypass-approvals-and-sandbox` | `--permission-mode bypassPermissions --dangerously-skip-permissions --tools default` | 显式危险模式。Backend 可执行本地写工具；飞书侧写入仍必须经过本项目的 policy、approval、dry-run、幂等和 dispatch gate。 |

`context_access` 不跟随写权限开放。只要本地 DB 存在，Router 会收到受 `query_scope` 限制的 bounded snapshot 和 read-only SQLite URI；Task Session 已由 Messages 提供会话正文，只收到 read-only URI、allowed tables 与当前 task scope，不再复制 snapshot。Hermes `safe` 没有本地 file/SQLite 工具：Router 使用 snapshot，Task Session 使用 Messages；具备只读 SQLite client 的 backend 才能直接查询 `read_only_uri`。

### 非交互子进程下的重要语义

daemon 通过 `subprocess` 以无 TTY 方式调用 `hermes chat -q -Q`。在此模式下，Hermes **不会**弹出交互式危险命令审批；对 `terminal()` 等路径，非 gateway 场景下通常会 **自动放行**（见 Hermes `tools/approval.py`）。

因此：

- `full_access` 是显式危险模式，会启用 Hermes 完整 CLI 工具集（file、terminal、browser、skills、memory 等）并传 `--yolo`；Codex 会传 `--dangerously-bypass-approvals-and-sandbox`；Claude Code 会传 `--permission-mode bypassPermissions --dangerously-skip-permissions --tools default`。
- 飞书侧真正的写保护来自：结构化 JSON schema、`answerability`/置信度等级/风险 gate、owner 审批、`dry-run`、幂等和 dispatch 策略。
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

Codex 只按 `agent_backend.codex.skills` 中的名称生成原生显式调用，例如新 Task Session 业务 prompt 最后一行的 `$docmate`。skill 必须由部署环境预先安装到 Codex 原生发现范围；本程序不读取 `SKILL.md`、不注册任意路径，也不模拟 Codex 的仓库根发现算法。完整 skill 是否加载由 Codex 决定，配置只表示 requested/configured。运行中的旧 session 不因配置变化自动增加 skill，需要新建或显式重置 session。生产 prompt、输出 schema 和 audit response 不增加 skill trace 字段。

Codex Task Session 还会通过 CLI `-c developer_instructions=...` 传入固定的非交互约束：工具探索保持静默，只返回一个最终 JSON 对象。该约束属于 Codex adapter，不进入 backend-neutral 业务 prompt，也不用于 Router、judge、reply postprocess 或 owner style refresh；`--output-schema`、`--output-last-message` 和 Python schema 校验仍共同约束最终输出。

生产 Task Session 输入使用紧凑的 Markdown 混合格式，固定顺序为 Instructions、Reply Context、Messages、非空 Resources、可选 Context Access、Output Contract，Codex 新 session 的显式 skill 引用随后占最后一行。Messages 是飞书正文的唯一嵌入来源；Reply Context 只保留 current/root id、合法回复目标和非空 chat type；Context Access 不复制消息 snapshot；空 Resources section 和 null message 字段完全省略。Output Contract 是模型可见的精简字段约束，最终 JSON 仍由 provider-native schema（支持时）和 Python schema 双重校验。Router、reply postprocess 和 owner style refresh 保持各自现有输入格式。

Reply postprocess 和 owner style refresh 也是 `hermes chat -q -Q` 一次性调用，但固定使用 read-only tool policy：

```text
--toolsets safe --max-turns reply_postprocess.max_turns
```

它们不传 `--resume`，不注入 native skill 请求或 `agent_backend.explicit_context.paths`，也不改变 task session。`reply_postprocess.model/provider` 为 `null` 时继承所选 backend 支持的 model/provider 配置。

`reply-style refresh --dry-run` 只拉取和过滤 owner 样本，输出计数和基础字符统计，不调用 Hermes、不写 profile。非 dry-run 在样本数达到 `min_samples` 后调用 summarizer，成功后通过临时文件原子替换 `owner_style.profile_path`。

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
