# P10 Current Docs Alignment Plan

## Summary

P10 是一个小文档修正计划。目标是清理旧计划文档中仍暗示 `waiting_approval` 属于 task lifecycle 的表述，避免后续新上下文或 UI 设计误读当前状态模型。

## Background

当前产品语义已经确认：approval 是阻塞自动发送的 Approval Blocker，不是 task lifecycle。`tasks.status` 不应再包含 `waiting_approval`，pending approval 也不应改变 task status。

实现者在全新上下文中应先阅读：

- `CONTEXT.md`
- `docs/plans/operator-surface-outline.md`
- `docs/plans/p6-state-lifecycle.md`
- `docs/specs/feishu-shadow-agent-mvp-design.md` 中 ApprovalRequest / 配置 / 后续迭代相关段落

## Dependencies

- 无代码依赖。
- 可独立于 P11-P14 提前执行。

## Goals

- 明确 `tasks.status` 不包含 `waiting_approval`。
- 明确 approval 是 blocker，不是 task lifecycle。
- 明确 pending approval 不改变 task status。
- 明确 `approved` / `rejected` / `expired` approval 是历史状态。
- 修正旧 P2/P3 计划中会误导后续实现的表述。

## Non-goals

- 不改代码。
- 不改 schema。
- 不重新设计 approval lifecycle。
- 不顺手清理与 `waiting_approval` 无关的旧计划文本。
- 不把 Product Policy Store、Operator Query、Operator Command 的新设计提前写进旧 P2/P3 计划。

## Suggested Prompt

```text
在 /Users/wufei2/github.com/wufei-png/feishu-shadow-agent 中只做文档修正，不改代码。

目标：修正旧计划文档里与当前状态模型冲突的表述。当前已确认语义是：
- tasks.status 不再包含 waiting_approval
- approval 是 blocker，不是 task lifecycle
- pending approval 不改变 task.status
- expired/rejected/approved approval 是历史状态
- status/replay/operator 视图展示 approval blocker，而不是把 task 标成 waiting_approval

请先用 rg 找出 docs/ 下仍提到 waiting_approval 或“mark task waiting_approval”的地方，重点检查 docs/plans/p2-ingestion-routing.md 和 docs/plans/p3-hermes-approval.md。
只修正文档漂移，不顺手改架构、不新增代码、不改 specs 中已经正确的内容。
修完运行：
git diff --check
并汇总改了哪些文件。
```

## Files To Update

- `docs/plans/p2-ingestion-routing.md`
- `docs/plans/p3-hermes-approval.md`
- Any other `docs/` file that still presents `waiting_approval` as a current task status.

## Handoff Notes

- 保留历史说明可以，但必须明确这是 removed legacy state。
- 不要把 expired/rejected/approved approval 写成 active blocker。
- 不要把 approval expiry 写成关闭 task 或触发 task-session 的事件。

## Test Plan

- Search all `docs/` references to `waiting_approval` / `waiting approval`.
- Confirm remaining hits are explicitly historical or describe removed legacy behavior.
- Confirm no code files changed.

## Acceptance

```bash
rg -n "waiting_approval|waiting approval" docs
git diff --check
```

Remaining hits are acceptable only when explicitly describing a removed legacy state.
