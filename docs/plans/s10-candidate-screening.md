# S10 单变量候选筛选记录

状态：**2026-09-21 两个候选均未采用；不进入会话 08 的候选对照或生产推广。** 生产 Task Session prompt 和 live follow-up 行为没有变更。

## 固定条件与假设

本轮只筛选一个带固定人工标签的、事实归属型 Task Session golden。基线和候选均使用 Codex / `gpt-5.6-luna`、`xhigh`、`read_only`、`repeat=1`，以及 run config hash `9b36317085cae9640ed299b5a62befdc784e91dcf1bc0cea245cfaf3c154578e`。case 原始 config hash 为 `98634ab11429d5c831f96ad30ed339973cb84cd7bcd0bf74d9104130e287ed46`；两次运行都使用同一 read-only 覆盖，故这个与 case 的既有差异不构成候选变量。

目标失败类别是事实型自动回复的关键归属遗漏和无依据扩展。唯一候选变量是在 `TASK_SESSION_INSTRUCTION` 添加一条规则：对实现、配置或指标归属问题，在可用时检查相关 read-only 证据，使每项事实可追溯，并将事实与未核实的可能性区分开。候选只在隔离工作树的 eval 中存在，未提交、未部署；评测没有调用 Dispatcher 或真实发送。

## 对照结果

| 指标 | 基线 | 候选 |
| --- | --- | --- |
| Task Session prompt hash | `22d37b7081f765df4d318c754fb1ba77ab357addd6ba8644d43effe2e15e2ee6` | `85f6b0eb61820c68960316be953d8442f4578cd7bd7046efbcd2f4b424ad4f16` |
| 结构评分 / answerability | 通过 / `auto_reply` | 通过 / `auto_reply` |
| semantic verdict | `partial`：1 minor omission、1 minor unsupported addition | `partial`：1 major contradiction、1 major omission、1 minor unsupported addition |
| owner 转交率 | 0 / 1 | 0 / 1 |
| Task Session prompt characters | 3149 | 3387（+238） |

候选没有改善主指标，反而将具体埋点路径断言为与标签相矛盾的来源，并扩大了遗漏范围。它也没有消除无依据的 endpoint 断言。结构通过和不增加转交不能抵消该语义回归，因此没有可提交给会话 08 的候选。

## 结论与下一假设

本轮不采用该候选，S10 保持开放。下一轮须先取得额外、独立人工标注且 production-visible 的事实归属样本；随后可检验更窄的假设：无法追溯具体 source/path 时，明确以 `needs_owner` 表示不确定性。该假设可能提高转交率，所以必须以语义差异和转交率一起筛选，不能因减少无依据断言而单独通过。

私有 case、配置副本和运行报告仍在 ignored 的 `data/evals/`；本记录只保留可复核的 hash、固定条件、汇总指标和结论。重跑时必须在隔离工作树临时应用上述唯一 prompt 差异，再通过 `eval run-task-session` 对相同 golden 执行 `repeat=1`；不得将临时 prompt 改动直接推广到生产。

## 2026-09-21 第二个单变量筛选（未采用）

来源校验修复后的同一事实归属 golden，在 Codex / `gpt-5.6-luna`、`xhigh`、`read_only`、
`repeat=1`、run config hash `9b36317085cae9640ed299b5a62befdc784e91dcf1bc0cea245cfaf3c154578e`
下重做基线与候选。目标是减少无依据的具体源码路径断言，同时保持可回答问题的自动回复。
唯一变量是在隔离工作树的 Task Session 指令中增加一条规则：具体 source/path 归属必须由
Messages、Resources 或实际查看的只读证据支持；否则说明不确定性并回答已核实的部分。
隔离差异仅一行，`prompt.py` 文件 SHA-256 为
`c9b00bd987287a3f2ab840fb135880a27b38ceacefdaeab4e7bfdca900262fbf`；没有提交或部署。

| 指标 | 基线 | 第二候选 |
| --- | --- | --- |
| Task Session prompt hash | `22d37b7081f765df4d318c754fb1ba77ab357addd6ba8644d43effe2e15e2ee6` | `3cf2e3e74319f3061c2e6d857e2b2023f7ba2aaad7062f27c502a7cfcbad8372` |
| 结构 / answerability | 通过 / `auto_reply` | 通过 / `auto_reply` |
| semantic verdict | `partial`：1 minor omission、1 minor unsupported addition | `partial`：新增 1 major contradiction，仍有 1 minor omission、1 minor unsupported addition |
| owner 转交 | 0 / 1 | 0 / 1 |
| 最终显式 prompt 字符 | 3149 | 3347（+198） |

候选仍把关键指标的埋点位置归到与人工标签相矛盾的源码位置，并新增未经证实的执行路径。
这违反预设的重大错误零新增条件，因此不采用，也没有进入生产变更。基线本轮报告记录的
skill trace `invalid_jsonl` 后续确认是 U+0085 被通用 `splitlines()` 误切的 parser 缺陷；
用修复后的 parser 对同一保存会话重建为 available。该工具问题不能解释或抵消语义回归。
两次均未调用 Dispatcher 或真实发送；未获得结构化 token、费用或完整 duration，
`repeat=1` 不给出稳定性结论。下一步仍须先取得更多独立、可信来源和人工标签，再形成新假设。
