# P9 Processing Service Split Plan

状态：**已完成（2026-08 刷新）**。目标 collaborators 已全部落地，拆分决策已定；本文保留为历史计划与边界记录。

## Summary

P9 是 P5-P8 行为稳定后的结构清理：在不改变产品语义的前提下缩小 `TaskProcessingService` 的体积与职责。

## 目标

- `TaskProcessingService` 保持为高层编排者。
- 稳定职责提取到聚焦 collaborators。
- 保持 prompt contract、reply gates、resource preflight 语义与 audit 行为。
- 让后续行为变更更容易 review。

## 已落地结果

以下 collaborators 已作为独立模块存在，并在 `TaskProcessingService.__init__` 中注入：

- `agent_invocation.py`：agent 调用重试、重试分类、延迟/错误日志。
- `context_access.py`（`ContextAccessBuilder`）：router 与 task session 的 context access、query scope 卡片。
- `resource_preflight.py`（`ResourcePreflight`）：资源状态检查、重试注入、`blocked_waiting_external` 映射。
- `task_session_runner.py`（`TaskSessionRunner`）：prompt 消息 id 选择、prompt 构造、output model 选择、schema 验证、session 级 agent 调用。
- `reply_postprocess.py` / `reply_style.py`：回复后处理与 owner style。
- `approval_cards.py` / `card_actions.py`：审批卡片与动作。
- `SendComposer`、`ApprovalService`：回复组装与审批/通知/升级（processing.py 内的小型 collaborator）。
- `revision.py`（撤回/编辑切片）：revision 影响评估。

## 最终决策（2026-08）

- 拆分到此为止：所有明确的行为边界均已提取，剩余的两个编排方法 `_run_task_router`（约 380 行）与 `_run_task_session`（约 770 行）按 P9 本意属于主服务——route dispatch、router/session 编排、决定调用哪个 collaborator、产出最终 `ProcessingResult`。
- 不再做纯重排：没有明确收益前不继续拆分（backlog 约束）。若未来出现具体痛点（如某个行为变更难以 review 或测试），再单独评估把 session 生命周期并入 `TaskSessionRunner` 或提取通知 collaborator。
- 新增行为仍按「服务编排 + collaborator 执行」的边界落地，例如撤回/编辑的 revision review 上下文在 `task_session_runner.py`、通知与门禁留在 `processing.py`。

## 非目标

- 无 schema 重设计。
- 无 policy/routing/dispatch recovery 语义变更。
- 无新 agent backend/provider。
- 无 broad store rewrite（store 只按需加窄 wrapper）。

## Store 边界

`SQLiteStore` 不做 wholesale rewrite；approval/action 事务边界保留在 store 方法中。

## Context Access 安全

`ContextAccessBuilder` 保持产品边界：Python 决定是否暴露 context access；prompt 语义使用但不作为本地副作用的 security boundary。

## 验收（已满足）

```bash
.venv/bin/python -m pytest -q
git diff --check
```

相关回归测试：`tests/test_processing_collaborators.py` 等聚焦测试随各 collaborator 落地。
