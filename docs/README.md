# 文档导航

## 选择运行方式

| 需求 | 文档 |
| --- | --- |
| 轮询 owner 的群内 @ 和私聊，使用项目审批与发送 | [影子助手首次准备与预演](getting-started.md) |
| 用项目接入飞书，交由 Codex 原生会话执行并由项目发布回复 | [Codex 直连 AI 同事](operations/codex-native-coworker.md) |
| 用 Hermes 自己的 Gateway、会话和子 Agent 接入飞书 | [Hermes Gateway AI 同事](operations/hermes-coworker.md) |
| 维护由 `daemon` 启动的 AI 同事，使用 `agent_direct` 或 `project` 发送 | [兼容 AI 同事运行与验证](operations/single-group-ai-coworker.md) |

三种主要方式和兼容入口的配置、数据及发送边界见[当前架构边界](architecture/current-boundaries.md)。

## 配置、测试与维护

- [配置参考](configuration.md)：项目配置、策略导入和 Agent backend 参数。Hermes Gateway 使用自己的 profile 配置。
- [测试方式](testing.md)：本地测试、各运行方式的预演和真实飞书验收入口。
- [评测](evals.md)：模型、路由和任务会话的评测流程。
- [main 合并门禁](operations/main-merge-gate.md)：仓库合入及门禁操作。
- [术语表](../CONTEXT.md)：项目、任务、策略和 operator surface 的术语。

## 设计与历史记录

- [当前架构边界](architecture/current-boundaries.md)描述现有实现的职责和扩展边界；具体运行参数以代码及各模式手册为准。
- `adr/` 记录架构决策及其修订；旧决策的适用范围见文首说明。
- `specs/` 包含 MVP 基线和 Operator Console 设计；draft 和历史基线的实现状态需结合代码及当前架构边界核对。
- `plans/` 保留分阶段实施方案、待办和验证背景。已完成的阶段计划是历史记录，不作为运行手册。
- `operations/*acceptance*.md` 和[原生运行时架构审查](operations/native-runtime-architecture-review-2026-11-19.md)记录特定时间的验证与发现；运行状态可能变化。
- `ai_chats/` 保存早期讨论，供查阅设计背景。
