# Post-MVP Backlog

当前范围遵循[架构边界](../architecture/current-boundaries.md)：本地、single-owner、polling ingest，由代码控制策略、审批和发送。S1–S8 的功能及 S9 评测工具已在 `9c342dd` 前落地；以下只列尚未完成的交付与现场验收。各会话的实施、停止条件和检查在各自文件中，执行时只需读取选中的计划。

## 执行队列

状态截至 2026-09-21。编号是默认顺序；会话 06 可在会话 01 后与现场验收并行安排，07 必须等 06，08 必须等 07。状态和证据在完成该会话时更新。

| 会话 | 交付 | 前置 | 状态 |
| --- | --- | --- | --- |
| [01 · 恢复 CI 基线](post-mvp/01-ci-baseline.md) | 修正过期 schema 测试与依赖审计，确认远端 CI | 无 | 已完成 |
| [02 · main 合并门禁](post-mvp/02-main-protection.md) | PR、必需检查与紧急绕过 | 01 CI 全绿 | 已完成 |
| [03 · membership 错误归因](post-mvp/03-membership-errors.md) | endpoint 专属分类与回归 | 01 | 已完成 |
| [04 · 运行时现场验收](post-mvp/04-runtime-acceptance.md) | S4 ingest 追赶与 S5 离群/重入时间线 | 03、获准测试 chat | S4 已通过；S5 部分通过（dry-run 边界保留，见 `docs/operations/runtime-acceptance.md`） |
| [05 · 合并转发子资源](post-mvp/05-merge-forward-resources.md) | 真实 CLI 能力验证、条件接入、现场验收 | 03、04 的资源/身份条件 | 已完成：container direct leaf image/file 接入；folder collection 保持不自动递归下载 |
| [06 · S10 golden 修复](post-mvp/06-s10-golden.md) | 有效时间线、人工标签、S9/P22 重跑 | 01 | 部分完成：时间线契约已落地，失效 P22 已废弃，2 个有效 S9 case 固定重跑均未通过；待重采有效样本和人工标签 |
| [07 · 单变量候选筛选](post-mvp/07-s10-candidate.md) | 固定条件下的回答质量对照 | 06 | 已完成：一个事实归属候选出现语义回归，未采用；不进入 08，S10 保持开放（见筛选记录） |
| [08 · 扩样本与生产判定](post-mvp/08-s10-validation.md) | 独立真实样本复验、条件推广 | 07 | 待执行 |

## 当前证据

- 会话 01 已完成：`e6d2f14` 修正当前 schema 生命周期契约，`f141afc` 阻止迟到轮询回滚编辑，`c830b1d` 将 anyio 升级至 4.15.1；[CI run 35505846110](https://github.com/wufei-png/feishu-shadow-agent/actions/runs/35505846110) 的所有阻断 job 和非阻断 Coverage 均通过。
- 会话 02 已完成：active ruleset [23727358](https://github.com/wufei-png/feishu-shadow-agent/rules/23727358) 仅匹配 `main`，要求 PR 和九项 CI checks；当前仅 `wufei-png` 与 `wufei2` 为 `always` bypass actors，日常仍按[合并门禁契约](../operations/main-merge-gate.md)走 PR。 [PR #26](https://github.com/wufei-png/feishu-shadow-agent/pull/26) 合入运维契约，[PR #27](https://github.com/wufei-png/feishu-shadow-agent/pull/27) 记录加入 bypass actors 之前的直推拒绝、缺 check 拒绝及完整 checks 后的普通合入验收；见[门禁验收记录](../operations/main-merge-gate-validation.md)。
- 会话 03 已完成：`84d79d0` 将 lark-cli 失败 JSON 和 membership 分类收紧为 bot 身份、受支持 endpoint 的结构化 `10002`；`234002`、`234040`、scope、资源不匹配、纯文本和解析失败都不再写入 `absent`。ingest/dispatch 只对确认缺席写入带 `error_code`/`error_endpoint` 的 runtime fact；产品策略、Policy Audit 和不确定发送恢复语义保持不变。
- 会话 04：S4 已完成现场验收。修复后在受控 1,001 条 `group_at_me` 源上保持 `30s` budget 与
  `20 pages / 1000 messages` cap，验证 deferred backlog、跨 tick/重启 cursor 恢复、checkpoint
  推进和全量追赶；详见[运行时验收记录](../operations/runtime-acceptance.md)。S5 保持部分通过，
  因为本会话不发送真实资源或回复。
- 本机 `config.yaml` 配置的 `.venv/node_modules/.bin/lark-cli` 当前为 1.0.96；最初 capability preflight 使用的 1.0.56 已完成升级复验。
- 会话 05 已完成：后续根因检查表明可见 `file_` 是 `<folder>` collection key，不是 leaf file；直接下载该 root 才得到 HTTP `500` / code `40009`。bot 能以同一 container ID 枚举 folder，并下载两个一级 leaf file。`_resources()` 已接入 merge_forward 的 direct leaf image/file（container ID），同时排除 `<folder>` / `is_folder` collection 键，避免错误调用和未受限递归获取。隔离真实资源链已在 Operator message detail 中验证 image 与一个 leaf file 都为 `downloaded`；详见会话计划、ADR-0013 和[运行时验收记录](../operations/runtime-acceptance.md)。
- [S9 记录](s9-answer-quality-baseline.md)中五个旧样本的当次失败仍可作为待复核线索。[P22 六轮 case](p22-task-session-context-budget-evidence.md)漏掉参考答案依赖的原始 owner 答复；旧三组 `0/1` 不能用于调参。生产上下文策略保持现状，S10 仍开放。
- 会话 06 的本地结构审计确认旧 P22 fixture 仅有 1 条 setup、5 条 target，未保存 owner 事实，且忽略的同窗来源没有可补充的 owner 消息。生产中关联任务的 owner 回复会触发 `human_taken_over`，不能作为 resumed Task Session 上下文补入；该 private golden 已从 active suite 移至 retired 区，不再进入任何 P22 重跑。新的真实样本仍需人工选择、核对可见性并完成标签。
- active Task Session suite 的 16 个 case 中，6 个匹配当前 owner；其中 4 个带 owner intervention，不能模拟为活跃 Task Session。剩余 2 个在 Codex / `gpt-5.6-luna`、`xhigh`、`read_only`、固定配置 hash 和 `repeat=1` 下重跑：1 个结构失败，1 个结构通过后 semantic judge 判出各 1 个 minor omission 与 minor unsupported addition，均未通过。P22 没有有效 case，以上不是完整质量基线或生产变更依据。
- 会话 07 固定一个带标签的事实归属 case，仅增加“可用时检查 read-only 证据、使事实可追溯”的 Task Session 指令作为候选变量。候选仍为 `auto_reply`，但 semantic judge 从 1 个 minor omission + 1 个 minor unsupported addition 恶化为 1 个 major contradiction + 1 个 major omission + 1 个 minor unsupported addition，且 prompt 增加 238 字符；候选未采用，不能触发会话 08。脱敏条件、hash、指标和下一假设见[筛选记录](s10-candidate-screening.md)。

## 需求触发的扩展

通用配置编辑、per-user policy、部署安装器、远程 Console、SDK/OAuth 主通道与向量检索不属于上述队列。启动前分别明确产品需求及配置变更、身份授权、审计和回滚语义，必要时更新架构边界。
