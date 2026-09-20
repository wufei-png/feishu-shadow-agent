# 01 · 恢复 CI 基线

状态：待执行。前置：无。代码起点为 `9c342dd`；执行前重新核对 HEAD、锁文件和最新 CI。

## 目标与现有证据

[CI run 35502877321](https://github.com/wufei-png/feishu-shadow-agent/actions/runs/35502877321) 的五个 Python 矩阵均因 `tests/test_message_lifecycle.py::test_schema_version_5_includes_message_type_column` 断言 schema v5 而失败；当前实际 schema 是 v7，同一单测已在本地复现。依赖审计还报告 `anyio 4.14.1` 的三项漏洞，并指向修复版本 4.14.2。静态质量、前端和包检查当时通过。以上均为 2026-09-20 的快照，须以执行时结果为准。

## 阶段

1. **恢复 schema 契约测试。** 检查 `store/schema.sql`、`store/migrate.py` 和相邻测试；把旧断言改为当前 schema 与 `message_type` 持久化的真实契约，保留受支持旧库迁移覆盖。验证该单测、schema/migration 测试和全量 pytest 后单独提交。生产 schema 不因过期测试而回退。
2. **恢复依赖审计。** 核对 `pyproject.toml` 与 `uv.lock` 的约束，升级 anyio 到经审计确认的修复版本；验证 locked 安装、依赖审计、全量 Python/前端/包检查后单独提交。若出现新审计项，按当次证据处理。

## 验收

运行 `uv run --locked pytest -q`、`uv run --locked ruff check .`、`uv run --locked ruff format --check .`、`uv run --locked --extra cards pyright`、`uv run --locked pip-audit --local --skip-editable --progress-spinner off`，以及 `AGENTS.md` 指定的前端和包检查。推送后确认 GitHub CI 的阻断 job 全绿；`Coverage (non-blocking)` 只记录结果。若远端 CI 仍失败，本会话保持未完成并记录失败 job。

每阶段显式暂存、检查 cached diff 与 `git diff --check`；只提交本阶段文件。完成后把提交、CI run 和状态写入 [当前待办](../post-mvp-backlog.md)。
