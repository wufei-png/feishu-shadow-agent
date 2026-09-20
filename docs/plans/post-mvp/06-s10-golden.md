# 06 · 修复 Task Session golden 与质量基线

状态：待执行。前置：会话 01 已完成；生产 Task Session 上下文策略保持现状。

## 目标与现有证据

S9 的五个旧样本曾给出过度转交、事实遗漏等当次失败；另一个 P22 六轮技术 case 的 `eval_case.yaml` 只有 1 条 setup 与 5 个 target，漏掉最终参考答案依赖的原始 owner 答复。旧 replay 以模型中间回答替代那些事实，因此 P22 三组 `0/1` 不能用于质量比较或调参。[S9 记录](../s9-answer-quality-baseline.md)和[P22 记录](../p22-task-session-context-budget-evidence.md)已标注证据失效。先修复可见上下文和标签，再重跑。

## 设计边界

评测时间线区分 target turn 与 context-only turn，保持真实消息顺序。context-only turn 不调用后端、不发送；它必须是生产 Task Session 真实可见的上下文。先核实 owner 消息的 takeover 语义：若原会话无法在生产状态机下忠实重放，弃用该 case，另采有效真实样本。兼容现有单 target、多 target case，不改变生产 prompt、schema 或上下文裁剪策略。

## 阶段

1. **评测契约。** 检查 `evals/schemas.py`、`cases.py`、`runtime.py`、`task_session_eval.py`。为 resume replay 增加按时间排序、可标明 target/context 的输入形式；验证 ID 唯一、时间顺序、source revision、资源引用与任务状态。`_judge_context()` 与实际 replay 使用同一事实时间线。测试缺/乱序 turn、owner 干预、fresh rebuild 与旧 case 兼容后单独提交。
2. **有效 case。** 在 ignored `data/evals/` 中核对原始 owner 答复的来源、时间和生产可见性；形成可供人工审核的标签清单。能够忠实重放才修复并重新 promotion，否则废弃旧 case、采集并 promotion 新 case。私有 fixture、消息、配置和 judge 原文不暂存；tracked 文档只写脱敏样本结构与审核结论。
3. **重新定基线。** 复核五个旧样本的可见上下文；固定 backend、model、reasoning、工具权限、配置 hash、标签与 `repeat=1`，重跑有效 S9 样本及 P22 策略。报告结构、语义、显式字符数和缺失的成本指标，更新 S9/P22 脱敏结论并单独提交。单次运行不代表稳定性。

## 验收

运行 eval schema/replay、prompt 形状及相邻测试，Ruff、Pyright、全量 pytest。检查新 case 的目标答案只依赖其时间点前可见事实，context-only 不额外调用模型，旧 case 保持有效。有效 case、人工标签和可复现固定条件齐备后才标记完成；否则保留阻断条件与 S10 待办。
