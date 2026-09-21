# 06 · 修复 Task Session golden 与质量基线

状态：部分完成。评测时间线契约已落地；旧 P22 golden 已因不可忠实重放而废弃；2 个结构有效 S9 case 已按固定条件重跑但均未通过。仍缺新的有效 P22 样本、人工标签和完整基线。前置：会话 01 已完成；生产 Task Session 上下文策略保持现状。

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

## 2026-09-21 来源与新增样本核查

- capture 现在先校验 Feishu 当前快照和 SQLite 当前语义 hash，再写 `source_revision`，同时记录具体 `source_task_ids`；带 context 的 replay 要求 context 与所有 target 共享生产 task ID。旧的只有布尔 membership 的私有捕获需重新采集，不能手动补 ID。
- 当前运行库以新 schema 重建后含 1,001 条受控测试消息，但没有生产 task 或 `task_messages`。因此不能从该库为旧 P22 生成可信的多轮 task 来源证明。active suite 中匹配当前 owner 的 6 个 case，4 个含 owner 接管；另外 2 个维持既有有效 S9 基线资格。
- 从真实飞书消息中补捕一个当前 owner 的独立单轮技术问题，保存为 ignored `data/evals/captured/` 的 review draft；`run-task-session --dry-run-backend` 通过结构检查。其唯一目标发生在 owner 后续回复之前，但缺少可独立核验的标准答案，未 promotion，也未用于调参或候选通过判定。原始消息和配置不进入 Git。
- 06 仍为部分完成：需要新的生产 Task Session 多轮来源和经审核的人工标签，才能建立有效 P22 基线；不得把测试群 marker、接管后的 owner 事实或模型中间回答填作生产可见上下文。
- 同两条有效 S9 case 在固定 `repeat=1` 条件下再运行，仍为 `0/2`：1 个结构失败，1 个结构通过但有 1 minor omission 与 1 minor unsupported addition；后一条报告最初记录 `invalid_jsonl`，后续已从同一保存会话确认是 parser 缺陷并恢复 skill trace。见[S9 脱敏记录](../s9-answer-quality-baseline.md)。

## 2026-09-21 现有 golden 可复用性盘点

active Task Session suite 共 16 个已 promotion case，均有历史 capture provenance 和人工标签，但都早于当前 `source_revision` / `source_task_ids` 契约；因此“已 promotion”本身不能满足新的生产来源门槛。

| 分类 | 数量 | 当前可用范围 | 限制 |
| --- | ---: | --- | --- |
| 当前 owner、无 owner intervention | 2 | 可做当前固定条件 S9 回归；本轮已重跑 | 1 个 initial、1 个 legacy resume；缺新的 revision/task 绑定，不能作为 P22 多轮来源证明 |
| 当前 owner、含 owner intervention | 4 | 不进入当前回归 | 生产会接管或忽略，按 Task Session target/context 重放不忠实 |
| 其他 owner、无 owner intervention | 9 | 仅可在各自归档 owner/config 下做历史代码回归 | 当前 runner 会因 owner 不匹配拒绝；同样缺新的 revision/task 绑定，不能计入当前质量基线 |
| 其他 owner、含 owner intervention | 1 | 不进入回归 | 同时存在 owner 不匹配和生产接管语义，无法忠实重放 |

两条当前可回归 case 只能回答“现有实现对这两份旧标签的本次表现”，不能证明长会话上下文有效、来源完整或质量稳定。其余 14 条不能扩充当前分母：9 条只保留历史配置回归价值，5 条因 owner intervention 失去 Task Session 重放资格。

`skill_trace` 的 `invalid_jsonl` 已用原产物定位为 parser 把 JSON 字符串内 U+0085 误当换行的工具缺陷。底层 JSONL 按 LF 逐行全部有效；修复后同一保存会话可重建为 `available`，无需重新调用模型。该修复只恢复技能证据，不改变当次结构或语义得分。

## 06–08 剩余准入条件

- **06：**至少取得 1 个可忠实重放的生产多轮 Task Session，所有 timeline 消息带匹配的 `source_revision`，context 与 targets 共享具体生产 task ID，且不含 owner 接管；参考答案和结构标签须由人依据目标时点前可见事实审核。随后在固定 backend/model/reasoning/工具权限/config/标签下重建 S9/P22 baseline。
- **07：**只在 06 的有效 golden 与固定 baseline 就绪后重开。每轮只改一个变量，预先定义目标失败和拒绝条件；此前两个候选均新增 major contradiction，不能作为后续候选或正证据。
- **08：**除 07 有通过筛选的候选外，还需至少 3 个相互独立、来源完整且完成人工标签的真实技术会话。固定 paired run 必须覆盖旧有效样本和新增样本；没有新增重大矛盾、无依据扩展或靠更多 owner 转交形成的表面收益，才具备生产推广资格。

在这些输入就绪前，06 保持部分完成，07 只保留两次拒绝记录，08 的当前判定保持不推广。
