# Feishu Shadow Agent 测试方式

本文说明当前项目的本地测试、分层验证和真实飞书端到端测试方式。默认测试应保持无副作用，不访问真实飞书，也不真实发送消息。

## 1. 本地测试

创建环境并安装开发依赖：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

运行完整单元测试：

```bash
python -m pytest -q
```

按模块收窄测试：

```bash
python -m pytest -q tests/test_config.py tests/test_lark_cli.py
python -m pytest -q tests/test_p2_ingestion_routing.py tests/test_p3_hermes_approval.py
python -m pytest -q tests/test_daemon.py tests/test_dispatcher.py tests/test_cli.py
python -m pytest -q tests/test_store_migrations.py tests/test_retention.py
python -m pytest -q tests/test_product_policy_store.py
```

提交前做基础格式卫生检查：

```bash
git diff --check
```

## 2. 测试覆盖分层

当前测试大多使用 fake client 或本地 SQLite 临时库：

- `tests/test_config.py`：配置 schema、严格布尔值、路径安全、资源限额默认值、脱敏输出。
- `tests/test_lark_cli.py`：`lark-cli` 命令构造、JSON 解析、dry-run banner、资源下载身份。
- `tests/test_p2_ingestion_routing.py`：消息 normalize、入口拉取、checkpoint、任务归属、资源下载限额。
- `tests/test_p3_hermes_approval.py`：Hermes 输出 schema、reply gate、审批队列、`/approve`、`/reject`、`/send`。
- `tests/test_daemon.py`：tick 顺序、heartbeat、运行中 health fail-closed、approval inbox 失败保护、dispatch 行为。
- `tests/test_dispatcher.py`：dry-run、真实发送、dispatch attempt、读回验证、stale sending 恢复。
- `tests/test_store_migrations.py`：SQLite migration、busy timeout、约束、状态 enum 契约、幂等动作。
- `tests/test_product_policy_store.py`：Product Policy Store 初始化探针、config import/replace、chat policy skip、audit old/new。
- `tests/test_retention.py`：消息 raw JSON 和资源保留策略。

这些测试验证代码契约，不证明当前机器的 `lark-cli` 授权、飞书权限或 Hermes 可执行文件可用；真实环境要跑下面的端到端流程。

## 3. 端到端测试准备

端到端测试建议只使用测试 P2P 会话或测试群，不要直接在生产群验证。

准备项：

- `config.yaml` 已从 `config.example.yaml` 复制并填写 owner open_id。
- `lark-cli auth status --verify --json` 在同一个 shell 环境下可通过。
- 飞书 bot 可私聊 owner。
- 如果要测群聊资源下载，bot 已加入测试群，且该群在 `chats` 中配置 `bot_joined: true`。
- `hermes --version` 和 `hermes status` 可执行。
- 测试群如需自动回复，配置 `auto_reply: true`；未知群默认只处理，不自动回复。

先运行无副作用健康检查：

```bash
python -m feishu_shadow_agent doctor --config config.yaml
```

Product Policy Store 初始化是显式 operator 动作，不会由 daemon 启动自动同步。需要验证 P12a 的持久化基础时先运行：

```bash
python -m feishu_shadow_agent policy import-config --config config.yaml
```

如需用当前 `config.yaml` 覆盖已有全局策略和其中列出的群策略，运行：

```bash
python -m feishu_shadow_agent policy import-config --config config.yaml --replace
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

- `data/agent.sqlite3` 中出现消息、任务、routing audit、Hermes audit 或 approval/action 记录。
- `logs/agent.jsonl` 中出现 `message_ingested`、`task_processing_completed`、`dispatch_action_previewed` 等事件。
- dry-run 模式不会真实对外回复；只有 owner notification 在显式加 `--send-owner-notifications` 时会真实发送。

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

`status` 和 `replay` 只读取本地状态，不推进审批过期。超过 `expires_at` 但尚未被显式推进的 approval 仍显示为 `pending`，并通过 `is_overdue`、`overdue_seconds` 和 `recommended_action: expire` 提示 operator。

如需显式推进 overdue approval 过期：

```bash
python -m feishu_shadow_agent maintenance expire-approvals --config config.yaml
```

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
