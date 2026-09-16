# S9 回答质量基线 — 2026-09-16 取证检查点

状态：**部分完成，长会话对照阻塞**。本文件只保存脱敏聚合结论与复现方法；真实消息、配置、数据库和原始报告仍位于被 Git 忽略的 `data/evals/`，不得提交。

## 范围与样本

- 当前运行库只有 3 条消息、1 个任务，且没有可用的 feedback、agent audit 或 processing failure，不能作为当前质量基线。
- 本机共有 15 个已 promotion 的 Task Session golden。按运行配置的 `owner.open_id` 校验后，5 个与当前 owner 匹配并纳入；另 10 个属于不同 owner，runner 均以配置不匹配拒绝，未计入质量分母。
- 5 个纳入样本均来自既有、人工确认过的真实飞书 capture：4 个 resumed（目标轮之前分别有 4、5、14、17 条任务消息），1 个 initial。resumed 目标 prompt 均只显式携带当前 1 条消息，历史依赖 provider session。
- 本轮没有 Router、Ingress 或资源型 current-owner golden，因此不能给出这些类别的当前通过率；也没有执行 Dispatcher 或真实发送。

脱敏规则：只记录数量、配置 hash、错误类别和字段级聚合；不记录聊天 ID、消息 ID、人员、原文、候选回复、参考答案或原始 judge 文本。

## 固定运行条件

| 条目 | 值 |
| --- | --- |
| 日期 | 2026-09-16 |
| uv / lark-cli | 0.12.4 / 1.0.56 |
| backend / model | Codex / `gpt-5.6-luna` |
| reasoning / tools | `xhigh` / `read_only` |
| repeat | **1** |
| eval-only timeout | 每次 Codex 子进程 300 秒；5 个计分运行均未触发 |
| run config hash | `93acdee8f9d74f591a3d9cb59a32c145a3d26da75a922fa60a55ebf28cf1fd3d` |
| case config hash | 5 个样本均为 `98634ab11429d5c831f96ad30ed339973cb84cd7bcd0bf74d9104130e287ed46` |

case 与 run 的配置被报告为 changed：历史 case 使用 `full_access`，当前运行使用 `read_only`，并增加了只用于避免评测无界等待的 300 秒超时。最终 5 个计分运行均在超时前返回，因此没有把 timeout 当成语义结果。

复现时先复制被忽略的本地 `config.yaml` 到仓库同级临时文件，只把 `agent_backend.codex.timeout_seconds` 设为 300，然后对每个 current-owner golden 单独执行：

```bash
uv run --locked python -m feishu_shadow_agent eval run-task-session \
  --config .s9-eval-config.yaml \
  --case data/evals/golden/task-session/<case> \
  --repeat 1 \
  --label s9-current-timeboxed
```

逐 case 执行可保留明确的失败边界。首个全目录尝试还证明：不同 owner 的 case 会被拒绝；无 timeout 的当前配置可能让单个模型子进程无界等待。该次中断批次不计入以下分母。

## 结果与归因

当前样本通过率为 **0/5**。由于 `repeat=1`，该结果只代表本次固定运行，不估计方差或稳定通过率。

| 分类 | 分母与结果 | 归因 |
| --- | --- | --- |
| 输出结构 | 3/5 通过 | 2/5 把 golden 期望的 `auto_reply + close` 判成 `needs_owner + keep_watching`；这是模型判断/过度转交，不是代码拒绝或发送结果 |
| 语义质量 | 0/3 scored 通过，3/3 为 partial | 共 5 个 omission（3 major、2 minor）、1 个 major contradiction、1 个 major unsupported addition |
| 早期事实保留 | 0/2 相关 resumed 样本通过 | 15 条和 18 条任务上下文的样本都遗漏前文关键排查事实；18 条样本还遗漏了早期给出的分段定位依据 |
| 技能可用性 | 5/5 available | 没有 skill 缺失，可排除“预期技能未加载”这一直接原因 |
| 运行错误/超时 | 0/5 | 5 个 candidate 均形成报告；3 个结构通过样本完成 judge，2 个结构失败样本按设计不评分；无 backend error、schema error 或 timeout |
| 路由/入口/资源/真实发送 | 未计分 | 缺少当前 owner 的对应 golden；本轮也未运行 Dispatcher，不能从 Task Session 结果推断线上漏收或发送成功 |

原始报告没有结构化 token、费用或端到端 duration 字段；人工观察的单 case 命令墙钟时间约 48–242 秒，只能作为运行可用性信号，不能作为可审计成本指标。prompt 字符数也未被当前 report 记录，因此 P22 的长度/成本对照仍缺指标。

## P22 与 S10 准入

18 条任务消息的真实 resumed case 已提供“早期事实在后续回答中遗漏”的失败信号，但它只有一次 setup 和一次 follow-up，不满足 P22 要求的 5–10+ 轮 agent 往返，也不能区分 provider session 衰减、重建窗口截断和模型注意力问题。当前证据不足以比较以下三个变量：

1. 现状（fresh 全量 / follow-up 单条）；
2. root + 最近 N、无 summary；
3. 有界窗口 + 系统组合 summary。

因此 S9 不能验收，S10 也不能启动。不能把 0/5 直接解释为应实施 context summary，也不能把现状判定为“无优化收益”。

## 阻塞证据与下一步

- `lark-cli auth status --json --verify` 显示 bot 身份 ready，但 user 身份缺少 token。
- `eval capture` 的候选搜索使用 user 身份，当前返回 `need_user_authorization`，需要 `search:message`、`im:message.reactions:read` 和 `contact:user.basic_profile:readonly`。
- bot 身份当前 chat list 为 0；尝试读取已知历史 chat 均返回 230002（bot 不在 chat），不能作为降级采集路径。

解阻后按以下顺序继续，所有模型评测仍固定 `repeat=1`：

1. 由用户完成 `lark-cli auth login`，再用 `lark-cli auth status --json --verify` 确认 user ready。
2. 用 `eval capture` 跨足够时间范围筛选至少一个 5–10+ 轮 agent 往返、后续明确依赖早期事实的真实线程；原始 capture 保持 ignored。
3. 人工完成标签与 promotion 确认，不能由自动评测替代。
4. 在同一 backend/model/config/case 上依次运行现状、root + 最近 N、窗口 + 系统组合 summary，每个变体 `repeat=1`；补记 prompt 字符数、事实保留、漂移、墙钟时间以及可获得的 token/费用。
5. 只有对照结果能归因到具体变量后，才允许 S9 验收并决定 S10 是实施优化还是记录不采用结论。
