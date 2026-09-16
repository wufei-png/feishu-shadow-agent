# Post-MVP Backlog

状态：2026-09-16 起按已确认顺序实施。本文件只记录当前未完成工作；S1–S9 已在独立验收和提交后移除。P22 已完成证据对照并保留为不采用结论的引用。

## 已确认的范围与决策

- 当前产品内的可靠性、前端体验、运维能力和回答效果优化全部纳入，按依赖与独立交付的顺畅程度推进。
- 保持单 owner、本地助手定位及[当前架构边界](../architecture/current-boundaries.md)。远程 console、多用户、部署安装器等扩展单独决策。
- 前端重组为中文“待我处理”工作台，技术 ID、错误码和必要术语保留原文；聚合发送结果不确定、阻塞/失败、待审批事项，保留专业详情入口。
- 草稿按审批 ID 在当前页面会话内隔离保存，切回时恢复，刷新后不保证保留。提交绑定审批对象及有效版本；过期草稿须重新核对，后台刷新不得覆盖编辑内容。
- 后续会话可只读分析本机运行记录与评测产物；tracked 文档仅保存脱敏结论和可复现方法，真实消息、配置、数据库及原始报告不入库。S9 已获准执行新消息采集和候选/judge 模型评测，所有运行固定 `repeat=1`；仍未授权 Dispatcher 或真实发送。
- 以下阶段继续独立验收并提交；实际证据要求超过 10 阶段时报告原因，调整未完成计划，不强行拼接无关工作。

## 检查基线与证据分级

本轮实施前基线：`1dbd4c2`。未拉取远端，未检查线上健康，未操作生产记录。

| 类别 | 当前证据 | 阶段 |
| --- | --- | --- |
| 已证伪的效果假设 | P22 的 root + 最近 N 与静态 summary 在有效真实样本上均为 0/1，未降低显式输入，也未改善质量；生产上下文策略保持不变 | S10 |
| 已确认的回答失败 | 既有样本存在过度转交、遗漏、矛盾及无依据扩展；P22 样本再次出现过度转交和语义方向错误 | S10 |

当前使用 `uv 0.12.4` 和 locked 环境。实施前 Python 全量为 **705 passed, 1 skipped**；S9 新增多轮 replay、eval-only fresh 重建变体及 prompt 字符记录，本阶段指定契约为 **86 passed**，Ruff lint/format 与 Pyright 均通过。最近前端检查为 **11 passed** 且 lint/typecheck/build 通过。S9 的真实消息、配置和报告均保持 ignored，未运行 Dispatcher 或真实发送。

## 实施阶段

依赖表示技术前置；编号表示默认实施顺序。每阶段包含必要的 UI/API/命令/存储纵向改动与测试，不能把阶段是否正确推迟到后续证明。PY、FE 检查缩写见文末。

### S10 — 证据支持的后端效果优化

依赖：S9。交付：针对已归因问题、经对照验证的优化，或有证据支持的不采用结论。

- 根据 [S9 基线](s9-answer-quality-baseline.md) 选择资源证据组织、回复表达和回答/转交判定等实际有效改动；每次改变一个可归因变量，保留安全、路由、审批回归。
- [P22 对照](p22-task-session-context-budget-evidence.md) 已否定当前 root+最近 N 和静态 summary 候选；生产上下文保持现状，除非新的单变量对照证明收益。
- 验收同时报告质量和成本：不能靠沉默或转交更多任务给 owner 来制造“误答减少”，不能把输入缩短直接视作效果改善。
- 收益未证实时保留现状并记录不采用结论；证据缺失则继续阻塞，不能以“无收益”关闭。其他已确认失败项未处理时不得整体关闭效果待办。
- 如归因产生多个独立改动或需要超过本计划的阶段数，报告证据并重排未完成阶段，不把所有效果工作塞入一个不可审阅提交。
- 检查：相关 PY/FE 契约测试、与 S9 相同条件的对照评测、最终完整检查；prompt/schema 改动覆盖字段形状与旧库升级。

当前下一步：新证据推翻了原先的上下文优化方向，先确认 S10 是在保持上下文不变的前提下，直接以现有有效 golden 对“过度转交/证据使用”做单变量 prompt 实验，还是先扩大真实技术样本覆盖；确认后再改代码。未完成项继续保留，不能用 P22 的不采用结论关闭整个效果待办。

## 外部依赖与独立待决策项

| 条目 | 状态及启动条件 | 完成条件 |
| --- | --- | --- |
| 合并转发子资源下载 | 外部依赖：lark-cli 暴露 merge_forward 原始 message_list/子消息 ID；先核验工具链能力 | 使用当前消息可见的子消息标识尽力下载，缺失或失败保留占位符；遵守 ADR-0013，不跨源 chat 抓取 |
| 通用配置编辑和 per-user policy | 独立待决策；当前 Policy/Settings 沿用，任意配置写入不纳入本轮 | 先定义配置变更审批、审计、回滚及用户语义，再单独设计实施 |
| 部署与外部集成扩展 | 独立待决策：LaunchAgent/systemd/Windows service、桌面/远程 console、SDK/OAuth、向量检索及细粒度资源分析 | 明确产品需求后逐项决策，必要时更新当前架构边界；本表不代表实施授权 |

## 执行检查与新会话交接

- **PY `<paths>`**：`uv run --locked pytest -q <paths>`，按阶段覆盖新增行为与实际变更的相邻契约。
- **FE**：`npm --prefix frontend/operator-console run lint`、`npm --prefix frontend/operator-console test`、`npm --prefix frontend/operator-console run build`。build 包含 TypeScript 检查并刷新 console_static，产物随对应阶段检查和提交。
- 每阶段运行 `git diff --check`，Python 改动补 Ruff lint/format 与 Pyright；只暂存该阶段明确路径，检查 staged diff 后提交。
- 最终运行 `uv run --locked pytest -q`、`uv run --locked ruff check .`、`uv run --locked ruff format --check .`、`uv run --locked --extra cards pyright` 及 FE。发布/打包改动追加 `uv run --locked python -m build` 和 wheel 静态资源验证。
- 用 `uv 0.12.4` 与 `uv sync --locked --extra cards` 校准环境，先复核既有格式问题；修正文档中与现行迁移能力冲突的说明，历史计划不重写。
