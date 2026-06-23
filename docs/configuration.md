# 配置参考

Feishu Shadow Agent 的唯一配置真相来源是 `src/feishu_shadow_agent/config.py` 中的 Pydantic 模型。`config.yaml` 启动时由 `ConfigService.load()` 校验；`schemas/config.schema.json` 是从同一模型生成的 JSON Schema，主要给编辑器、CI 和文档引用使用。

常用命令：

```bash
python -m feishu_shadow_agent config show --config config.yaml --redacted
python -m feishu_shadow_agent config validate --config config.yaml
python -m feishu_shadow_agent config schema
```

`config validate` 只校验 YAML 结构和 Pydantic 语义，不检查 Feishu、Hermes、SQLite 或 CLI 可用性。运行前完整健康检查仍使用 `doctor`。

## 顶层配置

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `owner` | object | 必填 | 单 owner 配置，用于审批通知和本地 operator 命令。 |
| `daemon` | object | 见下表 | daemon 轮询和消息回看设置。 |
| `health` | object | 见下表 | 运行时健康检查间隔和超时。 |
| `storage` | object | 见下表 | 本地 SQLite 和资源下载目录。 |
| `logging` | object | 见下表 | 本地 JSONL 日志路径。 |
| `lark_cli` | object | 见下表 | `lark-cli` 可执行文件和超时。 |
| `hermes` | object | 见下表 | Hermes 集成方式和调用参数。 |
| `reply_policy` | object | 见下表 | 全局自动回复策略。 |
| `chats` | map | `{}` | 按 Feishu `chat_id` 配置群级策略覆盖，例如 `oc_xxx`。 |
| `tool_permissions` | enum | `guarded_write` | Hermes 工具权限档位：`read_only`、`guarded_write`、`full_access`。 |
| `retention` | object | 见下表 | 本地数据保留时间。 |
| `debug` | object | 见下表 | 调试用持久化开关。 |

未知字段会被拒绝。真实密钥不要写入 `config.yaml`，只允许写环境变量名，例如 `hermes.api_key_env`。

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
| `logging.jsonl_path` | string | `logs/agent.jsonl` | JSONL 日志路径；相对路径基于配置文件目录解析。 |
| `logging.level` | `debug`/`info`/`warning`/`error` | `info` | 写入日志 sink 的最低级别。 |
| `logging.console` | bool | `false` | 是否同时把人类可读运行日志写到 stderr；不影响命令结果 stdout。 |
| `logging.text_path` | string/null | `null` | 可选普通文本日志文件路径；相对路径基于配置文件目录解析。 |
| `lark_cli.path` | string/null | `null` | 指定 `lark-cli` 路径；`null` 使用当前 `PATH`。 |
| `lark_cli.timeout_seconds` | int `> 0` | `30` | `lark-cli` 子进程调用超时。 |
| `hermes.mode` | `cli`/`http` | `cli` | 只影响 `doctor`/运行时 health 探测方式；**无论取何值，任务处理始终走本机 `hermes chat` CLI**。`http` 仅用于探测 Hermes gateway/API server 是否可达。 |
| `hermes.path` | string/null | `null` | `cli` 模式下指定 Hermes 路径；`null` 使用当前 `PATH`。 |
| `hermes.source` | string | `feishu-shadow-agent` | 传给 Hermes 会话和审计数据的来源标记；不能为空。官方建议第三方集成可用 `tool` 以从用户 session 列表中过滤；本项目保留自定义标签便于审计区分。 |
| `hermes.router_max_turns` | int `> 0` | `4` | Hermes 任务路由调用的最大 tool iteration 轮数（`--max-turns`）。 |
| `hermes.session_max_turns` | int `> 0` | `8` | Hermes 单任务会话调用的最大 tool iteration 轮数（`--max-turns`）。 |
| `hermes.model` | string/null | `null` | 可选模型覆盖；`null` 时沿用本机 Hermes 配置（`~/.hermes/config.yaml` 或 Hermes 内置默认）。 |
| `hermes.provider` | string/null | `null` | 可选 provider 覆盖；`null` 时沿用本机 Hermes 配置。 |
| `hermes.timeout_seconds` | int `> 0` | `60` | Hermes 子进程或 health 调用超时。 |
| `hermes.health_url` | string/null | `null` | 仅 `mode: http` 时使用；必须以 `http://` 或 `https://` 开头，HTTP 模式下必填。典型值为 `http://127.0.0.1:8642/health`。**不用于 chat/路由/会话调用**。 |
| `hermes.api_key_env` | string/null | `HERMES_API_KEY` | `mode: http` 时可选 Bearer token 环境变量名。Hermes 官方 API server 常用 `API_SERVER_KEY`；`/health` 端点通常无需认证，此字段主要留给需要鉴权的 health URL。 |
| `reply_policy.p2p_auto_reply` | bool | `true` | P2P 私聊在风险和置信度通过时是否允许自动回复。 |
| `reply_policy.default_group_auto_reply` | bool | `false` | 未在 `chats` 显式配置的群是否默认允许自动回复。 |
| `reply_policy.risk_level_max` | `low`/`medium`/`high` | `low` | 全局自动回复允许的最高风险等级。 |
| `reply_policy.confidence_threshold` | number `0..1` | `0.85` | 全局自动回复所需最低 Hermes 置信度。 |
| `chats.<chat_id>.name` | string | `""` | 方便 operator 识别的群名，不作为 chat_id。 |
| `chats.<chat_id>.auto_reply` | bool | `false` | 该群在所有 gate 通过时是否允许自动回复。 |
| `chats.<chat_id>.bot_joined` | bool | `false` | bot 是否已进群；影响 bot 回复和资源访问能力判断。 |
| `chats.<chat_id>.reply_identity` | `bot_preferred`/`bot`/`user` | `bot_preferred` | 群回复身份策略：优先 bot、强制 bot、或 user 代发。 |
| `chats.<chat_id>.allow_user_fallback` | bool | `true` | `bot_preferred` 无法用 bot 时是否允许 fallback 到 user。 |
| `chats.<chat_id>.resource_download` | bool | `true` | 是否允许保存该群消息中的可下载资源。 |
| `chats.<chat_id>.risk_level_max` | `low`/`medium`/`high` | `low` | 该群自动回复允许的最高风险等级。 |
| `chats.<chat_id>.confidence_threshold` | number `0..1` | `0.85` | 该群自动回复所需最低 Hermes 置信度。 |
| `retention.raw_message_days` | int `>= 1` | `30` | 原始消息 payload 保留天数。 |
| `retention.resource_days` | int `>= 1` | `30` | 下载资源文件保留天数。 |
| `debug.save_full_hermes_io` | bool | `false` | 是否保存完整 Hermes 输入输出；常规运行应保持关闭。 |

## 权限档位

`tool_permissions` 控制传给 Hermes 的 `--toolsets` / `--yolo` 参数，并叠加本项目的 reply policy、审批队列和 dispatch gate。它与飞书对外发送权限是两套机制。

| `tool_permissions` | Hermes CLI 参数 | 实际边界 |
| --- | --- | --- |
| `read_only` | `--toolsets safe` | Hermes `safe` 工具集：允许 web 搜索/抓取、vision、`image_generate` 等；**禁用** terminal、file、`execute_code` 等本地写操作类工具。不是“零副作用”——例如 `image_generate` 可能写入图片文件。 |
| `guarded_write` | `--toolsets hermes-cli` | 启用 Hermes 完整 CLI 工具集（file、terminal、browser、skills、memory 等），**不**传 `--yolo`。 |
| `full_access` | `--toolsets hermes-cli --yolo` | 同上，并显式跳过 Hermes 危险命令审批提示（`--yolo`）。仍受 Hermes hardline block（如 `rm -rf /`）约束。 |

### 非交互子进程下的重要语义

daemon 通过 `subprocess` 以无 TTY 方式调用 `hermes chat -q -Q`。在此模式下，Hermes **不会**弹出交互式危险命令审批；对 `terminal()` 等路径，非 gateway 场景下通常会 **自动放行**（见 Hermes `tools/approval.py`）。

因此：

- `guarded_write` **不能**理解为“Hermes 会在写操作前拦住并等人点确认”。它与 `full_access` 的主要差异是工具集范围（`safe` vs `hermes-cli`）以及是否显式 `--yolo`，而不是“有无交互审批”。
- 飞书侧真正的写保护来自：结构化 JSON schema、`answerability`/置信度/风险 gate、owner 审批、`dry-run`、幂等和 dispatch 策略。
- 若需要更严格的本地副作用控制，应使用 `read_only`（`safe`），而不是指望 `guarded_write` 触发 Hermes TTY 审批。

## Hermes 集成说明

### 调用方式

任务路由和任务会话统一构造：

```text
hermes chat -q <prompt> -Q --source <source> --toolsets <...> [--yolo] --ignore-rules --max-turns N [--resume <session_id>] [--model ...] [--provider ...]
```

- `-Q`：程序化模式，stdout 为模型最终回复，stderr 输出 `session_id:`。
- `--ignore-rules`：跳过 `AGENTS.md`、`SOUL.md`、memory 和预加载 skill 的自动注入，避免仓库/编辑器规则污染飞书任务 prompt。

### `mode: http` 的范围

`hermes.mode: http` **仅**改变 health 检查：对 `hermes.health_url` 发 `GET`，可选带 `hermes.api_key_env` 中的 Bearer token。任务处理、路由、会话恢复**始终**走 CLI，与 `mode` 无关。

## Schema

生成并查看 JSON Schema：

```bash
python -m feishu_shadow_agent config schema
```

仓库内提交的 `schemas/config.schema.json` 必须与当前 Pydantic 模型生成结果一致。修改配置模型后，应重新生成该文件并运行配置测试。
