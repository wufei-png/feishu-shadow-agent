# Feishu Shadow Agent 测试方式

本文说明当前项目的本地测试、分层验证和真实飞书端到端测试方式。默认测试应保持无副作用，不访问真实飞书，也不真实发送消息。

模型、router、task-session、full-chain 和 ingress 的评测工作流见
[`docs/evals.md`](evals.md)。

## 1. 本地测试

创建环境并安装开发依赖：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

交互式审批卡片测试需要额外安装官方回调 SDK：

```bash
python -m pip install -e ".[dev,cards]"
```

运行完整单元测试：

```bash
python -m pytest -q
```

运行 Ruff lint 和格式检查：

```bash
python -m ruff check .
python -m ruff format --check .
python -m pyright --pythonpath "$(command -v python)"
```

Pyright 以 strict 模式只覆盖本轮纳入基线的新模块；全仓既有类型债务不作为新功能的阻断项。CI 中 Ruff、全量 pytest、前端 build、migration、包构建/静态资源检查和 eval smoke 都是阻断项，coverage 仅报告、不阻断。

如需验证 git hook 行为，或提交前跑同一套 Ruff autofix/format hooks：

```bash
pre-commit run --all-files
```

按模块收窄测试：

```bash
python -m pytest -q tests/test_config.py tests/test_lark_cli.py
python -m pytest -q tests/test_card_actions.py
python -m pytest -q tests/test_p2_ingestion_routing.py tests/test_p3_hermes_approval.py
python -m pytest -q tests/test_daemon.py tests/test_dispatcher.py tests/test_cli.py
python -m pytest -q tests/test_store_schema.py tests/test_retention.py
python -m pytest -q tests/test_product_policy_store.py
python -m pytest -q tests/test_policy_runtime.py
python -m pytest -q tests/test_operator_query.py tests/test_operator_commands.py
python -m pytest -q tests/test_console_api.py tests/test_operator_query.py
python -m pytest -q tests/test_reply_style.py
```

Operator Console 前端验证：

```bash
npm --prefix frontend/operator-console install
npm --prefix frontend/operator-console run typecheck
npm --prefix frontend/operator-console run build
```

Operator Console 发布产物验证：

```bash
rm -rf dist build
npm --prefix frontend/operator-console ci
npm --prefix frontend/operator-console run build
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m build
python3.11 -m venv /tmp/feishu-shadow-agent-release-check
/tmp/feishu-shadow-agent-release-check/bin/python -m pip install dist/*.whl
/tmp/feishu-shadow-agent-release-check/bin/python -m feishu_shadow_agent console --help
python - <<'PY'
import zipfile
from pathlib import Path

wheel = next(Path("dist").glob("*.whl"))
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
    index = "feishu_shadow_agent/console_static/index.html"
    assert index in names, f"missing {index}"
    html = archive.read(index).decode("utf-8")
    for ref in html.split('/assets/')[1:]:
        asset = ref.split('"', 1)[0].split("'", 1)[0]
        path = f"feishu_shadow_agent/console_static/assets/{asset}"
        assert path in names, f"missing {path}"
PY
git diff --check
```

GitHub Release 应附加同一次 renderer build 后生成的 sdist 和 wheel。不要发布 GitHub Pages，不要在本阶段生成 Electron、Tauri、PyInstaller 等二进制。

P18 的本地浏览器视觉 QA 记录见 `docs/plans/p18-operator-console-health-release-qa.md`。

提交前做基础格式卫生检查：

```bash
pre-commit run --all-files
git diff --check
```

## 2. 测试覆盖分层

当前测试大多使用 fake client 或本地 SQLite 临时库：

- `tests/test_config.py`：配置 schema、严格布尔值、路径安全、资源限额默认值、脱敏输出。
- `tests/test_lark_cli.py`：`lark-cli` 命令构造、JSON 解析、dry-run banner、资源下载身份。
- `tests/test_p2_ingestion_routing.py`：消息 normalize、入口拉取、checkpoint、任务归属、资源下载限额。
- `tests/test_p3_hermes_approval.py`：Agent 输出 schema、answerability/decision-reason 组合、reply gate、审批队列、`/approve`、`/reject`、`/send`。
- `tests/test_card_actions.py`：四种审批操作的 Card JSON、approval 绑定、owner 校验、event-id 幂等、原子 command/feedback、daemon wake-up、连接健康与文本兜底。
- `tests/test_daemon.py`：tick 顺序、heartbeat、运行中 health fail-closed、approval inbox 失败保护、dispatch 行为。
- `tests/test_dispatcher.py`：dry-run、真实发送、dispatch attempt、读回验证、stale sending 恢复。
- `tests/test_store_schema.py`：SQLite current schema bootstrap、busy timeout、约束、状态 enum 契约、幂等动作；项目不升级或兼容旧 schema。
- `tests/test_product_policy_store.py`：Product Policy Store 初始化探针、config import/replace、chat policy skip、audit old/new。
- `tests/test_policy_runtime.py`：runtime resolver 从 Product Policy Store 读取、缺失全局策略 fail closed、DB policy 覆盖 YAML import source。
- `tests/test_operator_query.py`：OperatorQueryService 只读 dashboard/detail DTO、overdue 派生、effective policy、Policy Import Diff 和 audit history。
- `tests/test_operator_commands.py`：OperatorCommandService 审批/dispatch/maintenance/policy mutation 结果 shape、policy 直接写入和 audit actor/reason。
- `tests/test_console_api.py`：本地 Operator Console 的 token/Host 校验、dashboard/queue/detail/policy/settings/health API、静态资源 serving 和 `console` CLI 启动输出。
- `tests/test_retention.py`：消息 raw JSON、资源和反馈内容/元数据分阶段保留策略。

这些测试验证代码契约，不证明当前机器的 `lark-cli` 授权、飞书权限或所选 Agent CLI 可用；真实环境要跑下面的端到端流程。

## 3. 端到端测试准备

端到端测试建议只使用测试 P2P 会话或测试群，不要直接在生产群验证。

准备项：

- `config.yaml` 已从 `config.example.yaml` 复制并填写 owner open_id。
- `lark-cli auth status --verify --json` 在同一个 shell 环境下可通过。
- 飞书 bot 可私聊 owner。
- 如果要测群聊资源下载，bot 已加入测试群，且该群在 `chats` 中配置 `bot_joined: true`。
- 配置选择的 Hermes、Codex 或 Claude Code CLI 已登录且 readiness 检查可通过。
- 测试群如需自动回复，在 `config.yaml` 的 Policy Import Source 中配置 `auto_reply: true`。

如需测试交互式审批卡片，还要完成：

- 安装 `cards` extra，并在飞书应用中启用卡片交互回调能力。
- 设置 `interactive_cards.enabled: true`，YAML 只填写 `app_id_env` / `app_secret_env` 的环境变量名。
- 在 daemon 的 shell 中导出对应应用 ID 和密钥；不要把真实值写入配置或仓库。
- 确认官方 SDK 长连接能接收 `card.action.trigger`。连接未健康时 daemon 应继续运行，并发送包含文本命令的 owner notification。

Product Policy Store 初始化是显式 operator 动作，不会由 daemon 启动自动同步。首次运行 `doctor` 或 daemon 前先导入：

```bash
python -m feishu_shadow_agent policy import-config --config config.yaml
```

如需用当前 `config.yaml` 覆盖已有全局策略和其中列出的群策略，运行：

```bash
python -m feishu_shadow_agent policy import-config --config config.yaml --replace
```

如需直接修改运行时 Product Policy Store，使用 policy update 命令。合法变更通过 schema 校验后会直接写入并审计，例如暂停某个群自动回复：

```bash
python -m feishu_shadow_agent policy update-chat --config config.yaml --chat-id oc_xxx --auto-reply false --reason "pause chat"
```

扩大自动化、下载、bot joined 生效范围或 bot/user fallback 身份范围的变更同样直接写入并审计：

```bash
python -m feishu_shadow_agent policy update-global --config config.yaml --unknown-group-auto-reply true --reason "temporary trial"
```

如果启用 `reply_postprocess.owner_style`，先用 dry-run 检查 owner 样本数量和过滤结果；dry-run 不调用 Hermes，也不会写入 profile：

```bash
python -m feishu_shadow_agent reply-style refresh --config config.yaml --dry-run
```

样本数量满足 `reply_postprocess.owner_style.refresh.min_samples` 后再刷新 profile：

```bash
python -m feishu_shadow_agent reply-style refresh --config config.yaml
```

再运行无副作用健康检查：

```bash
python -m feishu_shadow_agent doctor --config config.yaml
```

如需验证 bot 能真实私聊 owner，再显式运行：

```bash
python -m feishu_shadow_agent doctor --config config.yaml --send-test
```

## 4. Dry-run 端到端

先用 dry-run 跑完整链路：

```bash
python -m feishu_shadow_agent daemon --config config.yaml --dry-run --send-owner-notifications
```

验证步骤：

1. 在测试 P2P 或测试群里发送一条会触发处理的消息；群聊需要直接 `@owner`。
2. 等待一个 tick，默认最长约 60 秒。
3. 查看状态：

```bash
python -m feishu_shadow_agent status --config config.yaml
```

4. 如知道 `message_id`，查看该消息的本地路由和 dispatch preview：

```bash
python -m feishu_shadow_agent replay --config config.yaml --message-id <message_id> --dry-run
```

预期结果：

- `data/agent.sqlite3` 中出现消息、任务、routing audit、Agent audit 或 approval/action 记录。
- `logs/agent.jsonl` 中出现 `message_ingested`、`task_processing_completed`、`dispatch_action_previewed` 等事件。
- dry-run 模式不会真实对外回复；只有 owner notification 在显式加 `--send-owner-notifications` 时会真实发送。
- dry-run 产生的 approval/action 带 dry-run provenance，切换到生产模式后不能复用；生产发送必须由生产运行重新生成 approval。

## 5. 真实发送端到端

确认 dry-run 结果正确后，在测试会话中运行真实发送模式：

```bash
python -m feishu_shadow_agent daemon --config config.yaml
```

建议验证三条主路径：

1. P2P 低风险问题：高置信时应以 user 身份回复。
2. 白名单测试群直接 `@owner`：满足 gate 时优先以 bot 身份回复，必要时按配置 fallback 到 user。
3. 高风险或低置信消息：不自动对外回复，应生成 owner notification 或 pending approval。

审批闭环可在 owner 与 bot 的 P2P 中输入：

```text
/approve <a_or_t_id>
/send <task_id> <最终回复>
/reject <a_or_t_id>
```

启用且连接健康时，还应验证卡片四条路径：直接发送建议、编辑后发送、不发送并继续关注、不发送并结束任务。重复提交同一个 callback event 不得产生第二条 command 或 feedback；非 owner 操作必须被拒绝。处理后在 Feedback 页面确认 outcome、decision reason 和原始/最终回复差异。文本命令在卡片关闭、断连或 SDK 不可用时仍应可用。

`status` 通过只读 OperatorQueryService 输出 daemon liveness、Product Policy 状态、pending approvals、active tasks、dispatch actions 和最近错误；`replay` 只读取本地状态并预览相关 dispatch。两者都不推进审批过期。超过 `expires_at` 但尚未被显式推进的 approval 仍显示为 `pending`，并通过 `is_overdue`、`overdue_seconds` 和 `recommended_action: expire` 提示 operator。

本地 operator mutation 命令通过 OperatorCommandService 输出统一 YAML 结果，顶层字段包括 `status`、`command`、`actor`、`target`、`changed`、`result`、`warnings` 和 `next_actions`。`approve` / `reject` / `send`、dispatch recovery 和 maintenance expiry 都使用这个结构；`status: applied` 或 `no_change` 返回退出码 0，其余失败类状态返回退出码 2。

如需显式推进 overdue approval 过期：

```bash
python -m feishu_shadow_agent maintenance expire-approvals --config config.yaml
```

本地 Operator Console 可用于查看 Dashboard、Approvals、Tasks、Dispatch、Feedback、Policy、Settings 和 Health，并通过本地 command facade 执行 approval、dispatch recovery、maintenance expiry 和 Product Policy import/update：

```bash
python -m feishu_shadow_agent console --config config.yaml --host 127.0.0.1 --port 8765
```

启动后使用 stdout 中带 `token` 的本地 URL 打开浏览器。`/api/*` 只接受该进程生成的 bearer token；console 默认只绑定 loopback host，不写 `config.yaml`。Policy 页面仍通过 `OperatorCommandService` 调用与 CLI 一致的 `policy import-config` / policy update 命令边界。Health 展示规范化诊断 issue 和失败摘要，不作为 raw log viewer。

发送后检查：

```bash
python -m feishu_shadow_agent status --config config.yaml
python -m feishu_shadow_agent replay --config config.yaml --message-id <message_id> --dry-run
```

重点确认：

- action 状态从 `pending` 变为 `sent`、明确 `failed`，或不确定时变为 `failed_needs_review`。
- 发送 action 先有 dry-run 结果，再有真实发送结果，并在 `dispatch_attempts` 中记录本次 claim。
- 读回验证记录了 `sent_message_id`、`reply_to_message_id` 和 mentions。
- owner 直接在原 chat/thread 接管任务后，未发送的 `send_reply` action 会被取消，pending approval 会过期。

如果 action 进入 `failed_needs_review`，先本地查看证据：

```bash
python -m feishu_shadow_agent dispatch inspect --config config.yaml --action-id <action_id>
```

确认飞书侧消息已发送且有 `message_id` 后，用读回验证标记已发送：

```bash
python -m feishu_shadow_agent dispatch mark-sent --config config.yaml --action-id <action_id> --sent-message-id <om_xxx>
```

确认未发送或愿意人工重试时，显式重新入队；该操作保留原 idempotency key：

```bash
python -m feishu_shadow_agent dispatch retry --config config.yaml --action-id <action_id>
```

不再处理的发送动作可取消，取消会释放同 task/target 的 active send 约束：

```bash
python -m feishu_shadow_agent dispatch cancel --config config.yaml --action-id <action_id>
```

## 6. 故障复查

常用排查命令：

```bash
python -m feishu_shadow_agent config show --config config.yaml --redacted
python -m feishu_shadow_agent status --config config.yaml
python -m feishu_shadow_agent dispatch inspect --config config.yaml --action-id <action_id>
python -m feishu_shadow_agent replay --config config.yaml --message-id <message_id> --dry-run
tail -n 100 logs/agent.jsonl
```

资源下载失败时，优先看 `status` 和日志中的 `bot_not_joined`、`bot_invisible`、`resource_download_failed`。这类问题通常需要确认 bot 是否在群里，以及该群的 `bot_joined` / `resource_download` 配置。

资源被本地磁盘安全策略挡住时，会看到 `too_large` 或 `quota_exceeded`。这两类状态表示文件已被删除、`resources.path` 已置空，并且 task session agent 默认不会被调用；先调整 `storage.max_resource_bytes` / `storage.max_resource_dir_bytes` 或清理 `storage.resource_dir`，再人工决定是否重放相关消息。

dispatch 恢复只提供本地 CLI，不提供 owner bot DM 命令；不确定真实发送是否发生时，系统不会自动重发。
