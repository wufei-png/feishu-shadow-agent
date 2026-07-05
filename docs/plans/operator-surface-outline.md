# Operator Surface Implementation Outline

日期：2026-07-01

本文是下一轮 Operator Surface / UI 后端契约的总纲。目标不是直接做页面，而是先把 UI 需要依赖的产品策略、查询模型和命令边界固定下来。`context_access` 不在本轮范围内。

每个阶段文件必须能在全新上下文中独立执行：计划本身要写清背景、依赖、非目标、文件范围、验收命令和需要回查的源文件。实现者不应依赖当前会话记忆才能完成或验证该阶段。

## 1. Fixed Decisions

- `context_access` 本轮不处理。
- `waiting_approval` 已废弃；approval 是 blocker，不是 task lifecycle。
- 纯查询不得推进 approval expiry，也不得产生其他写副作用。
- approval expiry 只由 daemon tick、approval command 前置处理，或显式 maintenance/approval expire 命令推进。
- overdue approval 在 read model 中仍显示为 `pending`，额外派生 `is_overdue` / `overdue_seconds` / `recommended_action`。
- Product Policy Store 是运行时产品策略真相来源。
- `config.yaml.reply_policy` 和 `config.yaml.chats` 保留，但只作为显式 import/replace 来源，不是运行时策略源。
- DB global product policy 未初始化时，daemon fail-closed；operator 必须显式运行 `policy import-config`。
- `policy import-config` 默认只填缺失；`--replace` 覆盖 config 中声明的 policy，但不删除 DB 中存在而 config 缺失的 chat policy。
- Product Policy 变更必须写 `policy_audits`。
- `config.yaml` 与 Product Policy Store 的比较叫 Policy Import Diff，不叫 config drift；它只解释显式 import/replace 会产生的变化。
- OperatorCommandService 是统一门面，内部按领域拆成 Approval / Dispatch / Maintenance / Policy command services。

## 2. Plan Split

| 阶段 | 计划文件 | 目标 |
| --- | --- | --- |
| P10 | `docs/plans/p10-current-docs-alignment.md` | 修正文档漂移，移除旧 `waiting_approval` 语义 |
| P11 | `docs/plans/p11-approval-expiry-boundary.md` | 把 expiry 写副作用移出查询路径 |
| P12a | `docs/plans/p12-product-policy-store.md` | 建立 DB Product Policy Store、import/replace、audit、health foundation |
| P12b | `docs/plans/p12b-policy-runtime-cutover.md` | 把 runtime `PolicyResolver` / daemon 从 YAML 切到 Product Policy Store |
| P13 | `docs/plans/p13-operator-query-service.md` | 建立稳定 UI/read model DTO 和只读查询服务 |
| P14a | `docs/plans/p14-operator-command-service.md` | 建立 operator command 门面，包住 approval/dispatch/maintenance 现有命令 |
| P14b | `docs/plans/p14b-policy-command-updates.md` | 增加 policy update 命令和 audit 写入 |
| Spec | `docs/specs/operator-console-ui-system.md` | 固定本地 Operator Console 的 UI 系统、渲染栈和交互边界 |
| Spec | `docs/specs/operator-console-local-api.md` | 固定本地 console API、安全模型、route contract 和 Query/Command 映射 |
| Spec | `docs/specs/operator-console-settings-catalog.md` | 固定 console-exposed Settings Catalog 字段映射 |
| P15 | `docs/plans/p15-operator-console-foundation.md` | 建立本地 console runtime、FastAPI shell、Vite/React/TS scaffold 和静态资源打包基础 |
| Spec | `docs/specs/operator-console-screen-flows.md` | 固定最终 Operator Console 页面流、页面职责和分阶段页面实现顺序 |
| P16 | `docs/plans/p16-operator-console-core-screens.md` | 实现 Dashboard / Approvals / Tasks / Message Detail / Dispatch 核心操作页面 |
| P17 | `docs/plans/p17-operator-console-policy-settings.md` | 实现 Policy / Settings / Policy Import Diff / Policy Audit History 页面 |
| P18 | `docs/plans/p18-operator-console-health-release.md` | 实现 Health 诊断页面、视觉 QA 和 GitHub Release readiness |

## 3. Recommended Order

1. P10 Current Docs Alignment
2. P11 Approval Expiry Boundary
3. P12a Product Policy Store Foundation
4. P12b Policy Runtime Cutover
5. P13 Operator Query Service
6. P14a Operator Command Service Facade
7. P14b Policy Command Updates
8. Operator Console UI System Spec
9. Operator Console Local API Spec
10. Operator Console Settings Catalog Spec
11. P15 Operator Console Foundation
12. Operator Console Screen Flows Spec
13. P16 Operator Console Core Screens
14. P17 Operator Console Policy And Settings
15. P18 Operator Console Health And Release Readiness

依赖关系：

- P11 必须先于 P13，避免 QueryService 继承 `status_snapshot()` 的读时写副作用。
- P12a 必须先于 P12b，避免 runtime resolver 依赖尚未落地的 store/import/audit API。
- P12b 必须先于 P13，避免 UI read model 先绑定 YAML policy 后再切 DB policy。
- P14a 放在 P13 后，因为 command 返回值和 UI 操作反馈应复用稳定的 operator DTO 和 policy status 语言。
- P14b 放在 P14a 后，避免 policy update 写入/audit 逻辑和 command facade 基础设施混在一个大阶段。
- Operator Console UI System / Local API / Settings Catalog specs 必须先于 P15，避免前端 scaffold、API shell 和 settings 表达在实现中临时发明。
- P15 放在 P10-P14 后，因为它依赖已固定的 QueryService、CommandService 和 Product Policy Store 边界。
- Operator Console Screen Flows spec 必须先于 P16，避免最终页面职责和分期边界在代码实现中临时扩散。
- P16 放在 P15 后，因为它依赖本地 console runtime、renderer scaffold、settings catalog route 和 `message_detail` read model。
- P17 放在 P16 后，因为 Policy / Settings 页面应复用 P16 已建立的导航、query/mutation、命令反馈和 detail 交互模式。
- P18 放在 P17 后，因为 release readiness 和视觉 QA 应覆盖完整最终页面集，而不是在 Policy / Settings 未完成前过早冻结。
- P10 可独立提前做，工作量很小。

## 4. P10-P14 Global Non-goals

- 不做实际 Web UI / TUI 页面；P15 会在后续阶段开始本地 Operator Console foundation。
- 不处理 `context_access`。
- 不做旧 DB 兼容迁移；当前项目仍允许 clean baseline。
- 不做多 owner、多租户、权限账户模型。
- 不做 Feishu 交互卡片。
- 不做 SDK 替换。

## 5. P15-P18 UI Non-goals

- 不做远程 console、账号登录、多用户权限或外网访问。
- 不做 GitHub Pages；公开分发走 GitHub tags/releases。
- 不做 `config.yaml` 写回；v1 只允许 Product Policy 通过命令边界更新。
- 不做 risk levels 或 confirmation-required 二次确认流程。
- 不做动态 Settings schema engine；Settings Catalog 是稳定产品字段映射。
- 不做 Electron、Tauri、PyInstaller 等二进制打包；后续等 UI 稳定后单独评估。
- 不把 Health 做成默认 raw log viewer。

## 6. Fresh Context Contract

每个 plan 文件都必须包含：

- Summary / Background：该阶段为什么存在，以及它承接哪些已确认产品边界。
- Dependencies：必须先完成的阶段，以及实现前要读取的源文件。
- Goals / Non-goals：明确做什么、不做什么。
- Files To Update：预期代码、测试、文档范围。
- Test Plan / Acceptance：可直接复制执行的验证命令。
- Handoff Notes：哪些行为必须保持、哪些术语不能混用、哪些旧路径不能继续依赖。

计划之间可以共享术语，但单个实现上下文不应需要回看当前聊天记录才能知道验收标准。

## 7. Global Acceptance

每个实现计划完成时至少运行：

```bash
.venv/bin/python -m pytest -q
git diff --check
```

如果只改文档，可只运行：

```bash
git diff --check
```
