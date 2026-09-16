# S9 回答质量基线 — 2026-09-16

状态：**完成**。本文件只保存脱敏聚合结论与复现方法；真实消息、配置、数据库、golden、变体和原始报告仍位于被 Git 忽略的 `data/evals/`，不得提交。

## 范围与样本

- 当前运行库只有 3 条消息、1 个任务，且没有可用的 feedback、agent audit 或 processing failure，不能作为当前质量基线。
- 本机已有的 15 个 Task Session golden 中，5 个与当前 `owner.open_id` 匹配并纳入既有样本基线；另 10 个因 owner 不匹配被 runner 拒绝，未计入分母。
- 5 个既有样本均来自人工确认过的真实飞书 capture：4 个 resumed，1 个 initial。本轮未运行 Dispatcher 或真实发送。
- `lark-cli` user 授权恢复后，另采集并人工 promotion 了一个真实 P2P 技术讨论。它包含同一 Task Session 的 6 个连续目标轮，最终问题依赖前文的接口层级与模型路由事实，用于 P22 三策略对照。
- 一个先采集的运维进度样本因最终问题必须依赖后续 owner 才能提供的运行状态，不具备当时可回答的有效标签；该样本及标签校准运行全部排除。P22 技术样本的早期校准运行同样排除，只计最终固定标签后的三次运行。

脱敏规则：只记录数量、配置 hash、错误类别和字段级聚合；不记录聊天 ID、消息 ID、人员、原文、候选回复、参考答案或原始 judge 文本。

## 固定运行条件

| 条目 | 值 |
| --- | --- |
| 日期 | 2026-09-16 |
| uv / lark-cli | 0.12.4 / 1.0.56 |
| backend / model | Codex / `gpt-5.6-luna` |
| reasoning / tools | `xhigh` / `read_only` |
| repeat | **1** |
| eval-only timeout | 每次 Codex 子进程 300 秒；所有计分运行均未触发 |
| run config hash | `93acdee8f9d74f591a3d9cb59a32c145a3d26da75a922fa60a55ebf28cf1fd3d` |
| 既有 5 case config hash | `98634ab11429d5c831f96ad30ed339973cb84cd7bcd0bf74d9104130e287ed46` |
| P22 case config hash | `9b36317085cae9640ed299b5a62befdc784e91dcf1bc0cea245cfaf3c154578e` |

case 与 run 的配置被报告为 changed：历史 case 使用 `full_access`，当前运行使用 `read_only`，并增加了只用于避免评测无界等待的 300 秒超时。计分运行均在超时前返回，因此没有把 timeout 当成语义结果。

复现时复制被忽略的本地 `config.yaml` 到临时文件，只把 `agent_backend.codex.timeout_seconds` 设为 300，然后对单个 golden/variant 执行：

```bash
uv run --locked python -m feishu_shadow_agent eval run-task-session \
  --config .s9-eval-config.yaml \
  --case data/evals/<golden-or-variant>/task-session/<case> \
  --repeat 1 \
  --label <label>
```

逐 case 执行保留明确的失败边界。全目录尝试只用于确认 owner 隔离与 timeout 行为，不计入分母。

## 既有样本结果与归因

既有样本通过率为 **0/5**。由于 `repeat=1`，该结果只代表本次固定运行，不估计方差或稳定通过率。

| 分类 | 分母与结果 | 归因 |
| --- | --- | --- |
| 输出结构 | 3/5 通过 | 2/5 把期望的 `auto_reply + close` 判成 `needs_owner + keep_watching`；属于模型过度转交，不是代码拒绝或发送结果 |
| 语义质量 | 0/3 scored 通过，3/3 为 partial | 共 5 个 omission（3 major、2 minor）、1 个 major contradiction、1 个 major unsupported addition |
| 早期事实保留 | 0/2 相关 resumed 样本通过 | 15 条和 18 条任务上下文样本均遗漏前文关键排查事实 |
| 技能可用性 | 5/5 available | 没有 skill 缺失，可排除“预期技能未加载”这一直接原因 |
| 运行错误/超时 | 0/5 | 无 backend error、schema error 或 timeout |
| 路由/入口/资源/真实发送 | 未计分 | 缺少当前 owner 的对应 golden；未运行 Dispatcher，不能推断线上漏收或发送成功 |

## P22 长会话对照

三组只改变最终目标轮的上下文策略；前 5 轮均按正常 Task Session 顺序回放。固定标签期望最终轮 `auto_reply + keep_watching`，参考答案要求模型选择留在下层服务，并保留文本/纯图片路由、绕过 wrapper 的调用方及入库/query 一致性等早期事实。

| 最终轮策略 | prompt message 形状 | prompt 字符 | 结果 | 归因 |
| --- | --- | ---: | --- | --- |
| 当前 live session | 当前消息 1 条，provider session 自持历史 | 3099 | 0/1，结构失败 | 错判为 `needs_owner`，再次过度转交；未进入语义评分 |
| fresh root + 最近 2 条 | root、倒数第 2 条、当前消息 | 3545 | 0/1，结构通过、语义失败 | 1 major contradiction、1 major omission、1 minor unsupported addition |
| 同上 + 系统组合 summary | 3 条消息加仅含目标前事实的摘要 | 3775 | 0/1，结构失败 | 把 `keep_watching` 错判为 `close`；未进入语义评分 |

有界无摘要变体把结构判定从失败变为通过，但答案反向建议由 API wrapper 承担模型选择，并遗漏关键路由事实，不能视为收益。摘要变体又错误关闭任务。两种变体的显式请求分别比当前增加 446 和 676 个字符，未实现输入缩短。三组均为 **0/1**，所以没有证据支持把 root + 最近 N 或静态系统摘要投入生产，生产策略保持不变。

`prompt_chars` 只统计本次显式请求，不含 provider session 保留的历史及 token 成本。报告仍没有结构化 token、费用或端到端 duration；人工观察墙钟时间只能作为运行信号，不作为可审计成本指标。

## S10 准入结论

S9 已形成可复现基线和可归因失败，满足准入条件；它没有证明任何上下文裁剪方案有效。S10 不实施 P22 的 root + 最近 N 或静态 summary 假设，除非新的一变量对照重新证明收益。

仍需处理的确认失败是：

1. 多个样本把可直接回答的问题转交 owner；
2. 即使结构通过，也可能出现关键事实遗漏、方向相反的回答和无依据扩展；
3. 当前指标不能审计完整 token、费用和耗时。

下一阶段应保持生产上下文策略不变，用同一有效 golden 对“回答/转交判定与证据使用”做一次只改变一个变量的实验；若先扩大样本覆盖，则先补充相同人工标签边界的真实技术会话。任何候选仍须 `repeat=1`、不运行 Dispatcher，并同时报告结构、语义和显式输入长度。
