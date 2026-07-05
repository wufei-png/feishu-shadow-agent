# Feishu Shadow Agent

<p align="center">
  <img src="docs/assets/covers/shadow-assistant-night-office.png" alt="Feishu Shadow Agent Cover" width="100%">
</p>

Feishu Shadow Agent 是一个本机运行的飞书个人办公影子助手。它定时读取群聊里 `@我` 的消息和 P2P 私聊消息，交给 Hermes 处理；低风险、高置信的回复可以自动发出，不确定、高风险或需要人工判断的内容会先通过 bot 私聊 owner 审批。

当前项目处于 MVP 阶段，核心形态是：

- `Python + SQLite + lark-cli subprocess`
- 长驻 `daemon/watch`，附带本机 Operator Console；不做 LaunchAgent、cron、systemd、远程 Web UI 或桌面二进制
- 飞书 user 身份负责读消息和必要的代发回复
- 飞书 bot 身份负责 owner 通知、审批入口、群聊自动回复和资源下载
- Hermes CLI 负责任务路由和单任务会话处理

更多设计背景见 [MVP 设计](docs/specs/feishu-shadow-agent-mvp-design.md) 和 [流程图](docs/specs/feishu-shadow-agent-flows.md)。

## 快速启动

要求 Python 3.11+，并确保本机已经可以使用 `lark-cli` 和 `hermes`。

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，至少确认：

- `owner.open_id` 是 owner 的飞书 open_id
- Policy Import Source 中需要自动回复的群配置了 `chats.<chat_id>.auto_reply: true`
- 需要下载图片/文件资源的群在 Policy Import Source 中配置了 `chats.<chat_id>.bot_joined: true`
- `lark_cli.path` 和 `agent_backend.hermes.path` 留空时会使用当前 `PATH`

完整配置项见 [配置参考](docs/configuration.md)；JSON Schema 见 [schemas/config.schema.json](schemas/config.schema.json)。

启动前先把 `config.yaml` 中的策略字段显式导入 Product Policy Store；daemon 运行时只读取 SQLite 中的 Product Policy，未初始化时会 fail closed：

```bash
python -m feishu_shadow_agent policy import-config --config config.yaml
```

如需用当前 `config.yaml` 覆盖已有全局策略和其中列出的群策略，使用 `--replace`。导入后再跑健康检查：

```bash
python -m feishu_shadow_agent doctor --config config.yaml
```

推荐先用 dry-run 跑 daemon。这个模式会执行拉取、路由、Hermes、审批和 dispatch preview，但不会真实对外回复；加上 `--send-owner-notifications` 后，owner 通知仍会真实发送，方便验证审批闭环。

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

本地 Operator Console 默认绑定 `127.0.0.1`，通过启动时生成的一次性 bearer token 访问。Console 覆盖 Dashboard、Approvals、Tasks、Dispatch、Policy、Settings 和 Health；它只通过本地 `/api/*` 调用 `OperatorQueryService` / `OperatorCommandService`，不直接读 SQLite，也不写 `config.yaml`。

从源码修改 `frontend/operator-console/` 后，需要运行 `npm --prefix frontend/operator-console run build`，把 renderer 重新写入 Python 包内的 bundled static assets。

## 测试

本地单元测试不会真实访问飞书或 Hermes：

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
pre-commit run --all-files
```

端到端测试需要真实 `lark-cli`、飞书 user/bot 授权、owner open_id、测试群或测试 P2P 会话，以及可用的 Hermes CLI。完整步骤见 [测试方式](docs/testing.md)。

## 发布产物

公开分发走 GitHub tag + GitHub Release。发布前先构建 renderer，再构建 Python sdist/wheel，确保 wheel 内包含 `feishu_shadow_agent/console_static/index.html` 和它引用的 `/assets/*` 文件：

```bash
npm --prefix frontend/operator-console ci
npm --prefix frontend/operator-console run build
python -m build
```

GitHub Release 应附加同一次构建生成的 source distribution 和 wheel；当前不发布 GitHub Pages，也不构建 Electron、Tauri 或 PyInstaller 二进制。

## 数据与日志

默认本地状态写入：

- SQLite：`data/agent.sqlite3`
- 下载资源：`data/resources/`
- JSONL 日志：`logs/agent.jsonl`

真实配置 `config.yaml`、运行数据和日志默认不应提交到 git。`config.example.yaml` 只保留非密钥示例配置。
