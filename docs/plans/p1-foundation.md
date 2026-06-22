# P1 Foundation 最小可验证切片

## Summary

- 将当前纯文档仓库搭成可安装、可测试、可诊断的 Python 本地 agent 骨架。
- 交付 `python -m feishu_shadow_agent`、配置 schema、SQLite migration/Store、JSONL 日志、`FeishuClient`/`LarkCliClient`、`doctor` 和 health-only/no-op `daemon`。
- 不实现消息拉取、任务归属、Hermes 任务会话、审批状态机、dispatch 或真实对外回复；只有 `doctor --send-test` 可显式发送 owner 测试通知。

## File Structure

```text
pyproject.toml
.gitignore
config.example.yaml
docs/plans/p1-foundation.md
src/feishu_shadow_agent/
tests/
```

## Module Boundaries

- `ConfigService` 只负责 YAML 加载、默认值、schema 校验、redaction；支持 `--config` 和 `FEISHU_SHADOW_AGENT_CONFIG`。
- `SQLiteStore` 只负责 migration、run/health/checkpoint 写入；不得依赖 Feishu/Hermes。
- `JSONLLogger` 追加写结构化日志，不写 secret。
- `LarkCliClient` 只构造 argv 和执行 subprocess，不写业务判断，不用 `shell=True`。
- `HealthSuite/doctor` 编排配置、SQLite、`lark-cli`、auth/scope、bot、owner 通知 dry-run 和 Hermes reachability。
- `daemon` 启动时跑完整 health；通过后只记录 no-op tick，不 ingest、不 send。

## Acceptance Commands

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
python -m feishu_shadow_agent --help
python -m feishu_shadow_agent config show --redacted --config tests/fixtures/minimal.config.yaml
python -m feishu_shadow_agent doctor --config config.yaml
python -m feishu_shadow_agent daemon --dry-run --config config.yaml
```

## Risks

- `lark-cli` flag 会漂移，doctor 运行时记录 path/version，测试固定当前 command builder surface。
- Codex shell/keychain 可能导致 `auth status --verify` 失败，应作为环境 health fail 暴露，不切 SDK。
- Hermes health endpoint 未最终固化，P1 只实现可配置 reachability，不实现 chat API。
- P1 建表和关键约束，但业务写 API 留到 P2/P3。

## Full Plan Details
# P1 Foundation 最小可验证切片

## Summary
- 目标：把当前纯文档 repo 搭成可安装、可测试、可诊断的 Python 本地 agent 骨架。
- P1 交付：`python -m feishu_shadow_agent` 入口、配置 schema、SQLite migration/Store、JSONL 日志、`FeishuClient`/`LarkCliClient` 命令封装、`doctor`、health-only/no-op `daemon`。
- P1 不做：消息拉取、normalize、任务归属、Hermes 任务会话、审批状态机、dispatch、真实对外回复；只有 `doctor --send-test` 可显式发送 owner 测试通知。

## File Structure
```text
pyproject.toml
.gitignore
config.example.yaml
docs/plans/p1-foundation.md

src/feishu_shadow_agent/
  __init__.py
  __main__.py
  cli.py
  config.py
  daemon.py
  health.py
  jsonl.py
  paths.py
  types.py
  feishu/client.py
  feishu/lark_cli.py
  store/sqlite_store.py
  store/migrations/0001_foundation.sql

tests/
  fixtures/minimal.config.yaml
  test_config.py
  test_store_migrations.py
  test_jsonl.py
  test_lark_cli.py
  test_doctor.py
  test_daemon.py
```
- 使用 `src/` layout、`argparse`、`sqlite3`、`subprocess`、`urllib.request`、`PyYAML`、`pydantic v2`、`pytest`。
- `.gitignore` 忽略 `config.yaml`、`data/`、`logs/`、`resources/`、虚拟环境和 Python 缓存。
- `config.example.yaml` 覆盖 `owner`、`daemon`、`health`、`storage`、`logging`、`lark_cli`、`hermes.health_url/api_key_env`、`reply_policy`、`chats`、`tool_permissions`、`retention`、`debug`；不包含任何 token/secret。

## Module Boundaries
- `ConfigService`：只负责加载 YAML、默认值、schema 校验、redaction；支持 `--config` 和 `FEISHU_SHADOW_AGENT_CONFIG`；真实密钥只能来自 env var 名称引用。
- `SQLiteStore`：只负责 migration、连接、基础 run/health/checkpoint 写入；不得依赖 Feishu/Hermes。`0001_foundation.sql` 建 `schema_migrations` 以及 spec 中 11 张核心表，落实唯一约束：`messages.message_id`、`tasks.short_id`、`approvals.short_id`、`actions.idempotency_key`、`checkpoints.key`、`task_watch_keys(task_id,key)`、`task_messages(task_id,message_id)`。
- `JSONLLogger`：追加写 `logs/agent.jsonl`，字段固定为 `ts`、`level`、`run_id`、`task_id`、`event`、`data`；创建目录，单行合法 JSON，不写 secret。
- `FeishuClient` protocol：定义 health/命令能力边界；业务层只依赖 protocol。
- `LarkCliClient`：只构造 argv 和执行 subprocess，不写业务判断，不用 `shell=True`；覆盖本机已确认的 `lark-cli 1.0.56` surface：`+messages-search`、`+chat-messages-list --order`、`+threads-messages-list`、`+messages-reply`、`+messages-resources-download`、`+messages-send`、`auth status --json --verify`。
- `HealthSuite/doctor`：编排配置、SQLite writable、`lark-cli` path/version、auth verify、user scopes、bot 可用性、owner 配置、owner 通知 dry-run、Hermes reachability。critical 失败返回 exit code `2`，warning 不阻断。
- `daemon`：启动时跑完整 health；critical fail 则 fail-closed；通过后进入 long-running no-op loop，只记录 run/health/no-op tick，不 ingest、不 send。测试直接调用 `run_one_noop_tick()`，不新增 cron/once CLI 模式。

## Test Checklist
- 配置：最小配置可加载；缺 owner/open_id、非法 `tool_permissions.profile`、chat policy 类型错误会失败；`config show --redacted` 不泄露 env key value。
- SQLite：migration 可重复执行；所有核心表存在；关键唯一约束生效；checkpoint upsert、run/health 写入可读回。
- JSONL：每行合法 JSON；必填字段存在；目录不存在时自动创建；异常对象可序列化为字符串。
- Lark CLI：argv 顺序和参数正确；互斥参数校验；资源 output 拒绝绝对路径和 `..`；subprocess 超时、非 0 exit、非 JSON stdout 都转为结构化错误。
- Doctor：用 fake runner 覆盖全绿、warning、critical fail、scope 缺失、Hermes 不可达、默认不发送；`--send-test` 才构造非 dry-run owner DM。
- Daemon：startup critical fail 不进入 loop；health 通过时记录 run 和 no-op tick；SIGINT/KeyboardInterrupt 能关闭 store/logger。

## Acceptance Commands
```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
python -m feishu_shadow_agent --help
python -m feishu_shadow_agent config show --redacted --config tests/fixtures/minimal.config.yaml
python -m feishu_shadow_agent doctor --config config.yaml
python -m feishu_shadow_agent daemon --dry-run --config config.yaml
```
- 最后一条为人工验收：看到 startup health 结果和 `daemon_tick_noop` 日志后手动 `Ctrl-C`。
- 当前本机基线已确认：`/Users/wufei2/.nvm/versions/node/v24.15.0/bin/lark-cli`，版本 `1.0.56`；实现仍以 live `doctor` 检查为准，不硬编码小版本。

## Risks And Defaults
- `lark-cli` flag 会漂移：command builder 测试固定当前 help surface，doctor 运行时记录 path/version，并把不兼容输出为 critical 诊断。
- Codex shell/keychain 可能导致 `auth status --verify` 失败：作为环境 health fail 记录，不改架构、不切 SDK。
- Hermes health endpoint 未最终固化：P1 只实现可配置 `health_url` reachability，不实现 chat API。
- schema 过早膨胀风险：P1 建完整表名和关键约束，但只开放 run/health/checkpoint Store API，业务写入留到 P2/P3。
- 误发消息风险：所有发送类 builder 默认 dry-run；`doctor --send-test` 是唯一显式真实发送入口，并要求 owner open_id 已配置。
