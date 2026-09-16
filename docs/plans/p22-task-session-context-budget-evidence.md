# P22 Task Session Context Budget — 证据先行与设计方向

状态：设计方向已定，**待真实长对话失败证据**后再定 schema/prompt/session 策略并实现。

## 背景

当前 Task Session 上下文策略（见 `task_session_runner.py`）：

- resumed session（`session_id` 存在）：prompt 只嵌入当前消息（`[current]`），历史由 provider session 自持；
- fresh session（无 session）：prompt 嵌入该 task 全部消息（`task_message_ids`）。

风险（群聊分析 §6 与 backlog）：

- 5–10 轮后 session 仍活着但早期关键信息只在 SQLite、不再注入，回答可能漂移；
- session 丢失后 fresh 重建对超长任务全量重放，成本高、噪声大。

仓库中现有 capture 最长 41 条消息。2026-09-16 的当前模型基线已在一个
18 条任务消息的真实 resumed case 上观察到早期事实遗漏，但该样本只有一次
setup 和一次 follow-up，仍**没有满足 5–10+ 轮 agent 往返要求的长对话对照样本**。
因此它是失败信号，不是窗口或 summary 方案的收益证据；详见
[S9 取证检查点](s9-answer-quality-baseline.md)。

## 已敲定方向（证据验证前的假设）

- **fresh 重建有界化**：root/首条 + 最近 N 条 + Task Running Summary（若存在），窗口大小可配置；不再全量重放。
- **follow-up 保持现状**：live provider session 的 follow-up 仍只发当前消息。
- **Summary 由系统组合生成**：从结构化状态（task_label、最近消息、决策/动作/审批记录）组合紧凑摘要，不增加 Agent 协议负担、不把 metadata 直接扩进生产 prompt。
- **注入点**：仅在 fresh 重建时注入（root + summary + 有界尾部）；不主动注入 live follow-up。

## 证据收集方法（下一步）

1. **样本**：采集一个真实长会话线程（5–10+ 轮 agent 往返：群线程或长 P2P），覆盖「早期给出的事实/承诺在后续轮被依赖」的结构；优先用真实运行数据而非合成。
2. **对照**：同一长会话分别用
   - 当前策略（fresh 全量 / follow-up 单条）做 baseline；
   - 有界重建（root + N + 无 summary）单变量；
   - 有界重建 + 系统组合 summary 单变量。
   固定同一 agent backend/model/run config，`repeat>=1`。
3. **观测指标**：早期事实是否被后续回答正确引用（结构+semantic）、prompt 输入字符数、预算溢出/截断事件、回答漂移（前后不一致）。
4. **失败归因**：区分「session 自持历史丢失/衰减」「重建窗口截断」「模型注意力/长度」三类，再决定窗口 N、summary 是否进生产 schema/prompt。
5. 结论落回本文与 backlog；变更前后运行 `pytest -q`、Ruff、`git diff --check`。

## 边界

- 不把 Task Running Summary 以 metadata 形式直接拼进生产 prompt；进生产前先经 eval 验证收益。
- 不改变 P21 已定的「Task Session 输入单一事实源」边界（ADR-0011）。
