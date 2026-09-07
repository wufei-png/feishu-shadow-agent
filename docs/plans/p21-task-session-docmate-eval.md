# P21 Task Session DocMate 评测与优化

状态：**已闭环（2026-08 刷新）**。代码、标注、promote、baseline 与多轮单变量对照均已完成；本文保留为交接入口、固定边界与历史结论。遗留一个 owner 决策（是否继续 DocMate 优化），见 [遗留决策](#遗留决策-owner)。通用评测契约、命令和产物结构以 [`docs/evals.md`](../evals.md) 与 [`p20-production-parity-model-evals.md`](p20-production-parity-model-evals.md) 为准。

## 目标

在不增加生产 Task Session 提示词和输出协议负担的前提下，评测 Hermes 是否会根据 skill description 自然加载 DocMate，并根据同一批 Task Session case 的真实失败分布优化命中策略或 DocMate 本身。

首要指标是现有 Task Session golden 的结构与语义通过率；技能命中率是诊断指标，不能取代最终回答质量。

## 已确定的边界

- Evaluation Run Config 使用 `tool_permissions: full_access`，与 owner 的默认生产用法一致。不要再为评测自动改成 `read_only`。
- Hermes 的 model/provider 保持 `null`，使用运行时默认模型。每次评测必须在报告或伴随记录中保存实际模型和 provider，不把某次观察到的型号写死。
- 通过 Hermes 全局 `skills.external_dirs` 暴露 `/home/SENSETIME/wufei2/.agents/skills` 下的全部技能；Task Session 不显式注入 DocMate。
- 先依赖 `docmate/SKILL.md` frontmatter 的 `description` 自然命中。基线证明存在漏命中或误命中后，优先优化 description；暂不修改 Task Session 提示词。
- DocMate 适用于项目文档、配置、API、实现行为、版本能力和需要仓库证据的故障排查。进度同步、人员协调、承诺和普通聊天不应命中。
- Hermes 保持 `full_access`；评测期间把 DocMate catalog 中的外部仓库当作只读证据源，不主动修改这些仓库。
- skill trace 只能是 eval 产物，不得加入生产 Task Session schema、生产提示词、飞书回复或生产 `agent_audits.response_json`。
- Task Session label 增加 `expected_skills`，默认空列表，且绝不发送给被评测 Agent。
- Full Chain 不在本轮改动范围内；Router 和 Ingress 只在发现 Task Session 根因实际位于上游时单独处理，不能混在同一对照实验中。

## 当前状态（2026-08 刷新）

- 代码全部落地并提交：`DraftTaskSessionLabels.expected_skills` / `TaskSessionLabels.expected_skills`（`evals/schemas.py`，去重并拒绝空名，旧 case 兼容）；eval-only skill trace（`evals/skill_trace.py`，接入 `evals/model_service.py`，区分 `preloaded_skills` 与 `runtime_loaded_skills`，非 Hermes backend 为 `unsupported_backend`）。生产 schema/prompt 无 trace 字段。
- 10 个 case 已 promote 到 `data/evals/golden/task-session/`，`labels.yaml` 含 owner 审核后的 `answerability`/`watch_action`/`reference_answer`/`expected_skills`（技术问答标 `[docmate]`，`p2p-wait-no-reply` 为空列表）。
- 2026-07-15 完成真实 baseline 与多轮单变量对照，完整实验记录在 ignored 的 `data/evals/P21_BASELINE_COMPARISON.md`（运行产物与报告一律不提交）。
- 另有 5 个 lark-monthly case（20260812 捕获、20260813 运行）作为独立月循环，不属于本计划的 10 case 范围。
- 当前机器即 Ubuntu 主机（`/home/SENSETIME/wufei2/go/src/github.com/wufei-png/feishu-shadow-agent` 及其 worktree）；如需 live capture/live ingress，运行前先确认 Lark CLI 登录状态。fixture eval 不依赖登录。
- ignored 的 `data/evals/TASK_SESSION_ANNOTATION.md`、`AI_PRELABEL_REVIEW.md` 仍含早期「16 个 case / read_only」表述，已过期；以本文为准则，本地不再据此行动。

## 实验结果摘要

- **Baseline（docmate-baseline，repeat=1）**：3/10 通过（`p2p-atlas-package`、`p2p-minicpm-start`、`p2p-wait-no-reply`）；DocMate recall 0/7，无误命中。三轮实际模型/provider 为 `gpt-5.6-terra` / `openai-codex`（易漂移，运行前重新记录）。
- **description 单变量（v1/v2，含 55 字截断验证）**：0/7 recall，无收益；已恢复 baseline 原值。Hermes 0.18.2 的 `extract_skill_description` 只保留 60 字符，v2 已规避截断，排除配置/缓存/同名覆盖/权限假象。**结论：不再修改 DocMate skill description 来解决命中问题。**
- **explicit_context.skills 机制**：有效。Hermes 原生 `--skills` 与 Codex 原生 `[$docmate]` 引用均可稳定预加载/激活 DocMate（trace recall 1.0），但不产生 `skill_view` 调用、不改善最终回答；eval trace 已能区分预加载与运行时加载，避免误报。
- **通用 prompt 候选（evidence gate、bounded wording、workflow 强制句、静默 developer instruction）**：能局部修正「未查证就 needs_owner」「一次性任务 keep_watching」结构偏置，但严格通过率仍为 0，且引入 60 秒 timeout 回归；全部撤回或仅保留 provider 级、不进业务 prompt 的项。**不继续增长 Task Session 提示词。**
- **timeout 语义**：60 秒失败是执行预算问题（180 秒对照 3/3 完成）；`agent_backend.*.timeout_seconds` 现支持正整数或 `null`（默认 `null` 不截断），daemon 仍按消息串行、由部署显式配置故障隔离上限。
- **保留的生产优化**：Task Session Markdown 分区输入（single authority，见 ADR-0011）、Session 级原生 skill 激活、紧凑 prompt（仅 Messages 嵌入正文）、Codex-only 静默 developer instruction、时间兼容判别（仅 answerability/watch_action 结构差异且已证明修复落地时可接受）。
- **剩余失败归因**：已从「DocMate 未命中」转为「命中后的证据收敛与答案忠实度」：package 读到正确 FAQ 后改坏命令；VPS/P2P 所需结论不在现有 DocMate FAQ/catalog；`p2p-pod-crash` 存在 capture-time reference 与 full-access 外部状态漂移（线上已修复时输出「当前已恢复」）。

## 遗留决策（owner）

若继续提升 DocMate 自然命中率，下一步不再是 description 单变量，选项包括：

1. 打破「先不修改 Task Session 提示词」的阶段边界：在通用 Task Session 提示中加入「回答前使用 Hermes 已提供的相关 skill」类约束（此前候选无稳定收益，需先定执行预算）；
2. 调整 Hermes/model 层的 tool-use 行为；
3. 改进 DocMate FAQ/catalog 覆盖（VPS/P2P 所需结论当前不在 FAQ 中），或把 skill 作为结构化、可执行的上下文单元暴露给 Codex 适配层。

以上任一项都会改变 P21 当前边界，须由 owner 明确确认后再做。未确认前，本计划视为关闭。

## 历史实施记录（已完成）

### 1. `expected_skills` label（已完成）

只扩展 Task Session draft/golden label：

```yaml
labels:
  reference_answer: "..."
  answerability: auto_reply
  watch_action: keep_watching
  expected_skills: [docmate]
```

要求（均已满足）：`list[str]` 默认 `[]`；校验去重并拒绝空名称；旧 case 不写该字段兼容；promotion 后保留；不扩展 production output model、不拼进 prompt；为 schema、promotion 和兼容性增加聚焦测试。

### 2. eval-only skill trace（已完成）

通过真实 Hermes session 记录事实，不让模型自报：

1. 从 Task Session `AgentRunResult.session_id` 收集本 trial 的 session id；resume 的 setup/target 可能属于同一 provider session，须去重。
2. trial 完成后调用 Hermes 公共接口导出（`hermes sessions export --session-id <id> --format jsonl --redact -`），解析实际工具调用，产出净化后的 `skill_trace`（status/expected/requested/runtime_loaded/requested_not_loaded/missing/unexpected/skill_view_calls/repository_reads）。
3. 只把净化摘要写入 `trials/<n>/report.yaml`；不保存完整 shell 命令、tool result、绝对用户目录、认证信息或未脱敏 export。
4. 无法导出时记录明确状态（dry-run backend 为 `unsupported_backend`），不伪造空轨迹。
5. 技能轨迹是诊断结果；最终 `passed` 仍由结构与 semantic judge 决定，另行汇总 precision/recall。
6. 优先复用 `TracedAgentBackend`；不直接耦合 Hermes `state.db` 私有 schema。

### 3. 文档与测试（已完成）

- `docs/evals.md` 的 Task Session label 与 trial report 示例已更新。
- 测试覆盖：旧 label 兼容、expected skill mismatch、resume session 去重、export 失败、redaction、非 Hermes/dry-run。

### 标注、Promote 与 baseline（已完成）

- Agent 前序工作：逐个读取 10 个 capture，复核 answerability/watch action/reference answer/scenario/expected_skills，高置信度直接填写，低置信度列审阅单；`--dry-run-backend --repeat 1` 结构预检。
- Owner 人工步骤：审阅低置信度标注并确认。
- 审核后：schema/引用/时间顺序/资源 SHA-256 预检 → 逐 case promote（不覆盖既有 golden）→ 真实 baseline → 单变量对照 → 候选稳定后 repeat=3 检查波动。全部完成，结论见「实验结果摘要」。

## 完成标准（核对结果）

- 当前 10 个 Task Session case 都有 owner 审核后的 golden label 和 `expected_skills`：**满足**。
- 生产 Task Session prompt/output schema 没有新增 trace 字段：**满足**。
- 每个真实 Hermes Task Session trial 有可用或明确失败原因的 eval-only skill trace：**满足**。
- 报告能区分最终回答失败、DocMate 漏命中、DocMate 误命中和仓库证据不足：**满足**（另加外部状态漂移与执行预算两类）。
- 至少完成一轮 baseline 与一个单变量优化后的对照评测：**满足**（多轮）。
- 所有测试、Ruff 和 whitespace 检查通过；代码和 tracked 文档提交，`data/`、本地 config 与真实运行产物不提交：**满足**（本文刷新后随 worktree 提交）。

## Suggested skills

- `docmate`：回答或核验项目文档、配置、API、版本和故障问题，并按 catalog 查阅 Ubuntu 上的相关仓库。
- `code-review-and-quality`：实现完成、提交前执行多轴代码 review。
- `handoff`：新会话再次中断或需要换 Agent 时更新交接摘要；不要把真实消息、token 或绝对个人信息写入 tracked 文档。
