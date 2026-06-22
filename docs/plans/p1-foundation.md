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
