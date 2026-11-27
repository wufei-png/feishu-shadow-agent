# Feishu Shadow Agent

<p align="center">
  <img src="docs/assets/covers/shadow-assistant-night-office.png" alt="Feishu Shadow Agent Cover" width="100%">
</p>

Feishu Shadow Agent 是运行在本机的飞书个人助手。它支持影子助手、Codex 直连 AI 同事和 Hermes Gateway AI 同事三种使用方式。

| 方式 | 消息与会话 | 运行入口 | 操作指南 |
| --- | --- | --- | --- |
| 影子助手 | 项目轮询 owner 的群内 @ 和私聊，维护任务、审批和发送队列；使用 Hermes、Codex 或 Claude Code 处理任务。 | `daemon` | [首次准备与预演](docs/getting-started.md) |
| Codex 直连 AI 同事 | 项目接收机器人事件并发布回复；Codex App Server 管理群、话题、私聊会话及原生子 Agent。 | `coworker run --runtime codex` | [Codex 直连](docs/operations/codex-native-coworker.md) |
| Hermes Gateway AI 同事 | Hermes 的 `coworker` profile 管理飞书连接、会话、回复和原生子 Agent。 | `coworker run --runtime hermes` | [Hermes Gateway](docs/operations/hermes-coworker.md) |

项目还提供由 `daemon` 启动的[兼容 AI 同事路径](docs/operations/single-group-ai-coworker.md)，支持模型直发或项目代发。每个项目 daemon 启动时选择影子助手或一种 AI 同事路径。

影子助手和 Codex 直连使用项目的 SQLite、策略及本地 Operator Console。Hermes Gateway 使用自己的 profile、配置和数据，项目启动入口不加载项目 SQLite 或 `config.yaml`；可选的[群聊伙伴集成](docs/operations/hermes-coworker.md#群聊伙伴)由 Hermes 适配器读取项目中的伙伴名单。同一飞书应用的多个长连接会分流事件；切换接收方前先停止原连接。

## 开始使用

项目启动入口需要 Python 3.11+ 和 uv 0.12.4；影子助手与 Codex 直连还需要 `lark-cli`。按上表选择指南，准备飞书应用及相应 Agent 运行环境。影子助手或 Codex 直连使用项目配置：

```bash
uv sync --locked --extra cards
cp -n config.example.yaml config.yaml
```

`cards` extra 提供官方飞书长连接 SDK，Codex 直连和兼容 AI 同事接收消息时需要它；影子助手启用交互式审批卡片时也需要。填写 `owner.open_id`、测试群和所选后端后，先导入策略并运行健康检查：

```bash
uv run --locked python -m feishu_shadow_agent policy import-config --config config.yaml
uv run --locked python -m feishu_shadow_agent doctor --config config.yaml
```

运行时从 SQLite Product Policy Store 读取已导入的策略。更新已有策略时，`policy import-config --replace` 覆盖全局策略和配置中列出的群策略，不删除仅存在于数据库中的其他群策略。

影子助手先执行预演：

```bash
uv run --locked python -m feishu_shadow_agent daemon --config config.yaml --dry-run
```

Codex 直连需要先在策略中启用 `coworker` 并选择 `provider: codex`，再执行：

```bash
uv run --locked python -m feishu_shadow_agent coworker run --runtime codex --config config.yaml --dry-run
```

两条预演路径使用与生产隔离的数据库、资源和日志，项目发送走预览；预演审批不能用于生产发送。影子助手及兼容 AI 同事的 Agent 仍可能通过自身工具产生外部写入，Codex 直连预演会隔离原生工具。去掉 `--dry-run` 即进入真实发送路径。Hermes Gateway 没有项目级 dry-run，启动和测试步骤见其运行指南。

已有预演库不会自动同步后续的生产策略修改；需要更新时运行 `policy sync-production --config config.yaml --mode dry-run`，该命令会覆盖预演策略。

启动本地 Operator Console 并查看对应状态：

```bash
uv run --locked python -m feishu_shadow_agent console --config config.yaml --mode dry-run
uv run --locked python -m feishu_shadow_agent status --config config.yaml --mode dry-run
```

Console 默认监听 `127.0.0.1`，使用启动时生成的 token。它展示项目持有的策略、任务、审批和运行状态；Hermes Gateway 的会话和状态使用 Hermes 自带的 CLI 或 Dashboard 查看。

完整配置见[配置参考](docs/configuration.md)，文档索引见[文档导航](docs/README.md)。

## 测试

本地测试默认使用 fake 客户端和临时 SQLite：

```bash
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked --extra cards pyright
uv run --locked pre-commit run --all-files
```

真实飞书测试需要授权的测试群或私聊。各模式的测试入口和范围见[测试方式](docs/testing.md)。

## 发布产物

GitHub Release 附加同一次构建生成的 source distribution 和 wheel。前端锁定的 Vite 要求 Node.js `^20.19.0 || >=22.12.0`。修改 Operator Console 后，先安装前端依赖并重新构建 renderer，再构建 Python 包：

```bash
npm --prefix frontend/operator-console ci
npm --prefix frontend/operator-console run lint
npm --prefix frontend/operator-console test
npm --prefix frontend/operator-console run build
uv run --locked python -m build
```

wheel 应包含 `feishu_shadow_agent/console_static/index.html` 及其引用的资源。当前不发布 GitHub Pages，也不构建桌面二进制。

## 数据与日志

项目默认将 SQLite、下载资源和 JSONL 日志分别写入 `data/agent.sqlite3`、`data/resources/` 和 `logs/agent.jsonl`。真实配置 `config.yaml`、运行数据、日志和凭据不提交到 Git；`config.example.yaml` 使用占位值。
