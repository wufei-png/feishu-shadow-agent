# P22 Task Session Context Budget — 对照结论

状态：**证据收集完成，当前不采用上下文裁剪方案**。生产 Task Session 上下文策略保持不变。

## 当前策略与原假设

当前 `TaskSessionRunner` 在 resumed session 只显式发送当前消息，历史由 provider session 自持；fresh session 则重放该 task 的全部消息。原假设是把 fresh 重建改为 root + 最近 N 条 + 可选的系统组合 summary，并让 live follow-up 保持当前单条模式。

该方向只是假设，不是已批准的生产设计。进入实现前必须证明它能保留早期事实、避免漂移，并在质量不退化的同时控制输入成本。

## 真实样本与对照

2026-09-16 通过已恢复授权的 `lark-cli` 采集并人工 promotion 了一个真实 P2P 技术会话。样本有 6 个连续 Task Session 目标轮，最终问题依赖早先给出的接口层级与模型路由事实。真实消息、标签和报告保持在被 Git 忽略的 `data/evals/`。

同一 backend、model、run config、case 和最终标签下，依次运行当前 live session、fresh root + 最近 2 条、同一窗口 + 目标前系统摘要。每个变体固定 `repeat=1`，结果如下：

| 策略 | 最终显式 prompt 字符 | 结构/语义 | 结论 |
| --- | ---: | --- | --- |
| 当前 live session | 3099 | 结构失败：`auto_reply` 被判为 `needs_owner` | 0/1 |
| root + 最近 2 条，无摘要 | 3545 | 结构通过；语义出现 major contradiction、major omission、minor unsupported addition | 0/1 |
| root + 最近 2 条 + 摘要 | 3775 | 结构失败：`keep_watching` 被判为 `close` | 0/1 |

有界重建没有降低显式输入长度，也没有提高最终通过率；摘要未挽回质量。无摘要变体虽然通过结构检查，却反向改变了模型选择责任层级并遗漏关键事实，因此不能把结构改善当成整体收益。

先采集的另一个运维进度样本因目标问题依赖未来 owner 状态而被判定为标签无效，其全部运行不计入结论。最终技术样本固定标签前的校准运行也不计入分母。

## 决策与边界

- 不把 root + 最近 N 或当前静态系统摘要加入生产 prompt/schema。
- 不改变 ADR-0011 的 Task Session 输入单一事实源边界。
- 保留当前 live follow-up 和 fresh 全量重建行为；本结论不是证明现状优良，而是证明两个候选在本次有效样本上没有收益。
- `prompt_chars` 不含 provider session 自持历史，报告也没有 token、费用和端到端 duration，因此不能声称完整成本更低或更高。
- `repeat=1` 只提供固定运行证据，不支持稳定性或方差判断。

详细失败归因、配置 hash 与复现方式见 [S9 回答质量基线](s9-answer-quality-baseline.md)。后续若重新研究上下文预算，必须引入新的单变量候选和有效真实样本，而不能直接实现本次被否定的假设。
