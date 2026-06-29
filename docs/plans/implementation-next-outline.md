# Feishu Shadow Agent Next Implementation Outline

日期：2026-06-29

本文是 P5-P9 的总纲计划索引。当前目标不是继续局部修补，而是把已经确认的产品语义、状态模型、dispatch 恢复和运行时安全边界拆成可交付计划，供全新上下文逐个实现。

## 1. Fixed Decisions

- 本轮不修改 `tool_permissions` 默认值；默认仍保持当前 `guarded_write`。未来如需收紧默认权限，应单独开安全专项。
- 当前项目未正式投入使用，不要求兼容旧 SQLite 数据。
- 实现这些计划时允许重写 `src/feishu_shadow_agent/store/migrations/` 为新的 schema baseline。
- 开发者应删除旧 `data/agent.sqlite3` 后重新初始化；测试只需覆盖新库初始化，不覆盖旧 schema upgrade。
- `reply_policy.default_group_auto_reply` 将被 `reply_policy.unknown_group_auto_reply` 取代，不保留旧字段兼容。
- `unknown_group_auto_reply` 只控制未知群是否允许自动回复；`resource_download`、`bot_joined`、`allow_user_fallback` 各自独立控制自己的问题。
- P2P single-active-task 不再是 deterministic shortcut；结构性 `reply_to` / `thread` 命中仍可确定性 attach。
- dispatch stale recovery 第一版只做识别和人工恢复，不做不确定状态下的自动重发。
- `tasks.status` 不再包含 `waiting_approval`；approval 是 blocker，不是 task lifecycle。
- `approvals.expires_at` 保留；`lifecycle.approval_timeout_hours: null` 表示永不过期，默认值为 `24`。

## 2. Plan Split

建议新增 **5 个实现计划 + 本总纲**：

| 阶段 | 计划文件 | 目标 |
| --- | --- | --- |
| P5 | `docs/plans/p5-policy-routing.md` | PolicyResolver、`unknown_group_auto_reply`、P2P routing shortcut 调整 |
| P6 | `docs/plans/p6-state-lifecycle.md` | Python `StrEnum`、DB `CHECK`、task lifecycle/blocker 拆分、approval expiry |
| P7 | `docs/plans/p7-dispatch-recovery.md` | dispatch attempt、`claim_token`、stale `sending` 人工恢复 CLI |
| P8 | `docs/plans/p8-runtime-resource-safety.md` | daemon heartbeat、SQLite busy timeout/WAL 评估、resource quota |
| P9 | `docs/plans/p9-processing-service-split.md` | `TaskProcessingService` 剩余职责拆分和瘦身 |

## 3. Recommended Order

按以下顺序实现：

1. P5 Policy And Routing
2. P6 State And Lifecycle
3. P7 Dispatch Recovery
4. P8 Runtime And Resource Safety
5. P9 Processing Service Split

依赖关系：

- P5 先落 `PolicyResolver`，避免后续 state 和 resource 改动继续复制 chat policy 逻辑。
- P6 先重建状态和 schema，P7/P8 再基于新的 `ActionStatus`、`ResourceStatus`、`runs` 字段扩展。
- P7 独立处理真实发送恢复，不和 P8 的运行时可靠性混在一起。
- P9 放在行为语义稳定之后做剩余结构化拆分；每个前置 plan 可以顺手抽自己必须用到的最小边界。

## 4. Recommended Commit Boundaries

- 推荐一个 plan 一个 commit，便于 review 和回滚。
- 如果某个 plan diff 过大，可以把 schema/config 变更和行为变更拆成两个 commit。
- 不要把纯重构和 DB/state 语义变更混在一起，除非该重构是当前 plan 的必要前置。

## 5. Global Non-goals

- 不做 SDK 替换。
- 不做 Web UI / TUI。
- 不做 LaunchAgent / systemd / cron 安装。
- 不做多 owner 或 per-chat owner。
- 不做 provider factory 或第二 agent backend。
- 不做旧 DB migration 兼容。
- 不做自动 dispatch 重发。

## 6. Global Acceptance

每个实现 plan 完成时至少运行：

```bash
.venv/bin/python -m pytest -q
git diff --check
```

如果只改文档，可只运行：

```bash
git diff --check
```
