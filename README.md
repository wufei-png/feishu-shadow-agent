# Feishu Shadow Agent

<p align="center">
  <img src="docs/assets/covers/shadow-assistant-night-office.png" alt="Feishu Shadow Agent Cover" width="100%">
</p>

Feishu Shadow Agent 是一个本机运行的飞书个人办公影子助手。它定时读取群聊里 `@我` 的消息和 P2P 私聊消息，交给可插拔的 Agent backend 处理；低风险、高置信的回复可以自动发出，不确定、高风险或需要人工判断的内容会先通过 bot 私聊 owner 审批。

当前项目处于 MVP 阶段，核心形态是：

- `Python + SQLite + lark-cli subprocess + pluggable agent backend`
- 长驻 `daemon/watch`，附带本机 Operator Console；不做 LaunchAgent、cron、systemd、远程 Web UI 或桌面二进制
- 飞书 user 身份负责读消息和必要的代发回复
- 飞书 bot 身份负责 owner 通知、审批入口、群聊自动回复和资源下载
- Hermes、Codex 或 Claude Code CLI 负责任务路由和单任务会话处理
- 可选官方飞书 Python SDK 长连接只接收 `card.action.trigger`；消息正文仍通过 user 身份轮询获取

当前扩展边界、Agent prompt 契约和 Context Access 信任边界见 [当前架构边界](docs/architecture/current-boundaries.md)；更多设计背景见 [MVP 设计](docs/specs/feishu-shadow-agent-mvp-design.md) 和 [流程图](docs/specs/feishu-shadow-agent-flows.md)。

## 快速启动

要求 Python 3.11+、uv 0.12.4，并确保本机已经可以使用 `lark-cli`，以及配置所选择的 Hermes、Codex 或 Claude Code CLI。

```bash
uv sync --locked
. .venv/bin/activate
cp config.example.yaml config.yaml
```

如需启用交互式审批卡片，同时安装可选的官方回调 SDK：

```bash
uv sync --locked --extra cards
```

编辑 `config.yaml`，至少确认：

- `owner.open_id` 是 owner 的飞书 open_id
- Policy Import Source 中需要自动回复的群配置了 `chats.<chat_id>.auto_reply: true`
- 需要下载图片/文件资源的群在 Policy Import Source 中配置了 `chats.<chat_id>.bot_joined: true`
- `lark_cli.path` 和所选 backend 的 `path` 留空时会使用当前 `PATH`
- 交互式卡片默认关闭；启用时只在 YAML 中配置凭证环境变量名，真实 `app_id` / `app_secret` 由环境变量提供

完整配置项见 [配置参考](docs/configuration.md)；JSON Schema 见 [schemas/config.schema.json](schemas/config.schema.json)。

启动前先把 `config.yaml` 中的策略字段显式导入 Product Policy Store；daemon 运行时只读取 SQLite 中的 Product Policy，未初始化时会 fail closed：

```bash
python -m feishu_shadow_agent policy import-config --config config.yaml
```

如需用当前 `config.yaml` 覆盖已有全局策略和其中列出的群策略，使用 `--replace`。导入后再跑健康检查：

```bash
python -m feishu_shadow_agent doctor --config config.yaml
```

推荐先用 dry-run 跑 daemon。这个模式会执行拉取、路由、Agent、审批和 dispatch preview，但不会真实对外回复；加上 `--send-owner-notifications` 后，owner 通知仍会真实发送，方便验证审批闭环。dry-run 中产生的审批和动作不会被提升为生产发送；生产模式必须重新生成审批。

```bash
python -m feishu_shadow_agent daemon --config config.yaml --dry-run --send-owner-notifications
```

确认行为符合预期后，再去掉 `--dry-run` 进入真实发送模式：

```bash
python -m feishu_shadow_agent daemon --config config.yaml
```

常用运维命令：

```bash
python -m feishu_shadow_agent status --config config.yaml
python -m feishu_shadow_agent replay --config config.yaml --message-id <message_id> --dry-run
python -m feishu_shadow_agent config show --config config.yaml --redacted
python -m feishu_shadow_agent config validate --config config.yaml
python -m feishu_shadow_agent config schema
python -m feishu_shadow_agent retention prune --config config.yaml --dry-run
python -m feishu_shadow_agent console --config config.yaml
```

本地 Operator Console 默认绑定 `127.0.0.1`，通过启动时生成的一次性 bearer token 访问。token 只放在 URL fragment 中（不会随 HTTP 请求发送），renderer 随后保存到当前 tab 的 `sessionStorage` 并立即清除 fragment；所有响应启用 no-store、no-referrer、CSP、nosniff 和禁止嵌入等安全头。Console 覆盖 Dashboard、Approvals、Tasks、Dispatch、Feedback、Policy、Settings、Health 和 Maintenance；Feedback 提供 7/30 天结果统计、决策原因切片和原始/最终回复差异。Console 只通过本地 `/api/*` 调用 `OperatorQueryService` / `OperatorCommandService`，不直接读 SQLite，也不写 `config.yaml`。Maintenance 是本机运维命令台，承载 doctor、config validate、retention prune 和 reply style refresh 等低频显式命令；审批队列清理仍放在 Approvals/Dashboard 的工作流语境中。

审批通知始终保留文本命令兜底。启用 `interactive_cards` 且回调长连接健康时，owner 还会收到绑定具体 approval 的四操作卡片：直接发送建议、编辑后发送、不发送并继续关注、不发送并结束任务。回调只接受配置 owner 的操作，事件 ID 用作幂等键；连接不健康时自动回退为纯文本通知。

每次 owner 处理审批都会写一条不可变反馈，区分建议直接发送、编辑后发送、不发送继续关注和不发送结束任务。反馈不会自动修改 Product Policy。默认保留敏感文本 30 天；到期只清空原记录中的敏感字段，保留最小审计元数据，不级联删除任务链记录。仍处于有效 `watch_until` 的 watching task 暂缓清理，具体可通过 `retention` 配置。

从源码修改 `frontend/operator-console/` 后，需要依次运行 `lint`、`test` 和 `build`；build 会把 renderer 重新写入 Python 包内的 bundled static assets。

## 测试

本地单元测试不会真实访问飞书或 Agent backend：

```bash
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked --extra cards pyright
uv run --locked pre-commit run --all-files
```

端到端测试需要真实 `lark-cli`、飞书 user/bot 授权、owner open_id、测试群或测试 P2P 会话，以及可用的所选 Agent CLI。交互式卡片还需要安装 `cards` extra、配置应用凭证环境变量并启用飞书卡片回调长连接。完整步骤见 [测试方式](docs/testing.md)。

## 发布产物

公开分发走 GitHub tag + GitHub Release。发布前先构建 renderer，再构建 Python sdist/wheel，确保 wheel 内包含 `feishu_shadow_agent/console_static/index.html` 和它引用的 `/assets/*` 文件：

```bash
npm --prefix frontend/operator-console ci
npm --prefix frontend/operator-console run lint
npm --prefix frontend/operator-console test
npm --prefix frontend/operator-console run build
uv run --locked python -m build
```

GitHub Release 应附加同一次构建生成的 source distribution 和 wheel；当前不发布 GitHub Pages，也不构建 Electron、Tauri 或 PyInstaller 二进制。

## 数据与日志

默认本地状态写入：

- SQLite：`data/agent.sqlite3`
- 下载资源：`data/resources/`
- JSONL 日志：`logs/agent.jsonl`

真实配置 `config.yaml`、运行数据和日志默认不应提交到 git。`config.example.yaml` 只保留非密钥示例配置。
