# 02 · main 合并门禁

状态：待执行。前置：会话 01 的最新 CI 全绿。

## 目标与现有证据

2026-09-20 读取 `main` protection 返回 404，ruleset 列表为空；仓库已有 CI，但没有合并门禁。此会话建立 PR 合并与必需检查规则，保留 single-owner 可审计的紧急绕过。执行前核对当前仓库权限、规则与 workflow job 名。

## 实施

1. 将规则、必需检查、紧急绕过和恢复步骤写入仓库运维文档，作为一个独立文档提交并通过 PR 合入。以最新 workflow 的实际 job 名设置必需检查：`Backend static quality`、五个 `Backend tests (Python …, …)`、`Operator Console build`、`Build and inspect package`、`Python dependency audit`。`Coverage (non-blocking)` 不设为必需；不为单 owner 仓库增加无法满足的第二审批人。
2. 合入说明后，用 GitHub 管理接口启用 main 规则。正常管理员提交也应走 PR；紧急绕过通过有记录的临时规则调整和恢复完成，避免永久 bypass actor 让日常直接推送畅通。核对规则读取结果，并用受控 PR 验证缺检查不可合并、检查齐全可合并；验证直接推送被拒绝。真实验证方式及结果写入文档，不把 API 配置成功等同于行为验收。

## 验收

记录规则 ID、必需 job 名、测试 PR、拒绝/允许结果、紧急绕过配置与启用时间。GitHub 规则是仓库外部状态；文档提交与远端验收共同构成本会话完成条件。若平台权限或规则能力不足，保持未完成并记录具体限制。更新[当前待办](../post-mvp-backlog.md)的状态和证据链接。
