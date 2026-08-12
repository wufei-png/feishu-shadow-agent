# P21 Task Session DocMate 评测与优化

状态：准备在 Ubuntu 主机落地。本文是新 Agent 会话的交接入口；通用评测契约、命令和产物结构以 [`docs/evals.md`](../evals.md) 与 [`p20-production-parity-model-evals.md`](p20-production-parity-model-evals.md) 为准。

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
- `context_trace` 只能是 eval 产物，不得加入生产 Task Session schema、生产提示词、飞书回复或生产 `agent_audits.response_json`。
- Task Session label 增加 `expected_skills`，默认空列表，且绝不发送给被评测 Agent。
- Full Chain 不在本轮改动范围内；Router 和 Ingress 只在发现 Task Session 根因实际位于上游时单独处理，不能混在同一对照实验中。

## Ubuntu 当前状态

- 仓库：`/home/SENSETIME/wufei2/go/src/github.com/wufei-png/feishu-shadow-agent`
- 已迁移到的提交：`0234276`。开始工作前仍需 `git pull --ff-only`，并保留用户已有改动。
- 未跟踪的运行配置：仓库根目录 `config.eval-ubuntu.yaml`。
  - Hermes native backend
  - Hermes 路径 `/home/SENSETIME/wufei2/.local/bin/hermes`
  - working directory 指向 Ubuntu 仓库
  - model/provider 为 `null`
  - `tool_permissions: full_access`
- Hermes default profile 已配置 `skills.external_dirs: /home/SENSETIME/wufei2/.agents/skills`，CLI 的 `skills`、`terminal`、`file` 和 `code_execution` toolset 当前均启用。
- 最近检查时 Hermes 默认模型为 `gpt-5.6-terra`、provider 为 OpenAI Codex；这是易漂移状态，运行前重新记录。
- `.venv` 使用 Python 3.11.13。
- `data/evals/captured/` 当前保留 10 个 case，共 70 个文件；迁移时两端 manifest 校验一致。不要恢复 owner 已删除的无用 capture。
- 10 个 case 已用 Ubuntu 配置通过 Task Session `--dry-run-backend` 构造预检。
- `data/evals/golden/task-session/` 当前没有 golden；真实 baseline 前必须先完成人工审核与 promote。
- Ubuntu 的 Lark CLI 尚未完成登录，因此当前 fixture eval 可运行，live capture/live ingress 暂不可运行。除非现有 fixture 被证明不足，否则不要重新 capture。
- `data/`、`config.eval-ubuntu.yaml`、真实消息、资源和运行报告都保持 ignored，不得提交。

`data/evals/TASK_SESSION_ANNOTATION.md` 和 `data/evals/AI_PRELABEL_REVIEW.md` 仍包含早期的“16 个 case”和“评测改用 read_only”表述，已经过期。新会话应以当前 10 个目录和本文的 `full_access` 决策为准，并在 Ubuntu 本地同步修正这些 ignored 审阅说明。

## 当前 case

以 Ubuntu 上实际目录为准：

- `p2p-atlas-package`
- `group-multimodal-false-positive`
- `group-vllm-version`
- `group-cache-off`
- `group-package-import`
- `group-deployment-param`
- `group-vps-bad-address`
- `p2p-minicpm-start`
- `p2p-pod-crash`
- `p2p-wait-no-reply`

已确认的两个 scenario 修订不得丢失：

- `group-deployment-param` 是 resume，setup 必须包含同一时间段内“升级包、先用 env 测试、好的、deployment 哪个位置”等前序消息；reference answer 是通过 Deployment 环境变量设置参数，例如 `kubectl set env deployment/<deployment-name> KEY=VALUE -n <namespace>`。
- `p2p-minicpm-start` 是 resume，setup 包含完整合并转发话题；文本中的关键 FlashInfer 报错必须可见。合并转发内不可下载的图片占位符不能阻塞已完整获取的文本，普通图片资源仍按原有资源预检处理。

## 待实现

### 1. `expected_skills` label

只扩展 Task Session draft/golden label：

```yaml
labels:
  reference_answer: "..."
  answerability: auto_reply
  watch_action: keep_watching
  expected_skills: [docmate]
```

要求：

- `DraftTaskSessionLabels.expected_skills` 和 `TaskSessionLabels.expected_skills` 均为 `list[str]`，默认 `[]`。
- 校验去重并拒绝空名称；旧 case 不写该字段时必须继续兼容。
- promotion 后保留该字段。
- 不扩展 production output model，也不把该字段拼进 prompt。
- 为 schema、promotion 和兼容性增加聚焦测试。

先由 Agent 根据完整原始消息和 DocMate 适用边界预标当前 10 个 case，再把低置信度项单独列给 owner 审核。技术问答不代表必然命中；只有需要 DocMate FAQ、文档或 catalog 仓库证据时才标 `[docmate]`。`p2p-wait-no-reply` 预期为空列表，但仍由完整上下文复核。

### 2. eval-only skill trace

通过真实 Hermes session 记录事实，不让模型自报：

1. 从 Task Session `AgentRunResult.session_id` 收集本 trial 的 session id；resume 的 setup/target 可能属于同一 provider session，须去重。
2. trial 完成后调用 Hermes 公共接口导出：

   ```bash
   hermes sessions export --session-id <id> --format jsonl --redact -
   ```

3. 解析实际工具调用，至少产出：

   ```yaml
   skill_trace:
     status: available | unavailable | unsupported_backend | export_error
     expected_skills: [docmate]
     requested_skills: [docmate]
     runtime_loaded_skills: [docmate]
     requested_not_loaded_skills: []
     missing_skills: []
     unexpected_skills: []
     skill_view_calls: 1
     repository_reads:
       - repository: <sanitized catalog name>
         paths: [<relative path>]
   ```

4. 只把上述净化后的摘要写入 `trials/<n>/report.yaml`；不要保存完整 shell 命令、tool result、绝对用户目录、认证信息或未脱敏 session export。
5. 无法导出时记录明确状态，不伪造空轨迹。Dry-run backend 应为 `unsupported_backend` 或等价的明确状态。
6. 技能轨迹是诊断结果。最终 `passed` 仍由现有结构与 semantic judge 决定；另行汇总技能 precision/recall 或 missing/unexpected counts。

优先复用 `TracedAgentBackend` 收集 Task Session 结果，并把 Hermes export/解析封装在 eval 模块内。不要直接耦合 Hermes `state.db` 私有 schema。

### 3. 文档与测试

- 更新 [`docs/evals.md`](../evals.md) 的 Task Session label 和 trial report 示例。
- 更新评测相关测试，重点覆盖：旧 label 兼容、expected skill mismatch、resume session 去重、export 失败、redaction、非 Hermes/dry-run。
- 变更完成后运行：

  ```bash
  python -m pytest -q
  python -m ruff check .
  python -m ruff format --check .
  git diff --check
  ```

提交前使用 `code-review-and-quality` skill 做一次多轴 review。

## 标注、Promote 与 baseline

### Agent 前序工作

1. 逐个读取当前 10 个 capture 的 `messages.jsonl`、`task_session.review.yaml`、相关资源及 `REVIEW.md`。
2. 根据目标消息当时可见的证据复核现有 answerability、watch action、reference answer、scenario 和 `expected_skills`。
3. 直接填写高置信度 label。
4. 生成一份 Ubuntu 本地审阅单，逐文件列出修改字段，只列低置信度或有争议的标注供 owner 重点审核。
5. 用 `--dry-run-backend --repeat 1` 做结构预检，不把 dry-run 当作模型能力评测。

### Owner 人工步骤

Owner 只需要：

1. 阅读低置信度审阅单。
2. 修改或确认相应 `task_session.review.yaml`。
3. 回复“Task Session 与 expected_skills 标注已审核完成”。

### 审核后

1. 再次做 schema、引用 message id、时间顺序和资源 SHA-256 预检。
2. 逐 case promote 到 `data/evals/golden/task-session/<name>/`；不得覆盖已有 golden。
3. 运行一次真实 exploratory baseline：

   ```bash
   .venv/bin/python -m feishu_shadow_agent eval run-task-session \
     --config config.eval-ubuntu.yaml \
     --cases data/evals/golden/task-session \
     --repeat 1 \
     --label docmate-baseline
   ```

4. 记录运行时 Hermes model/provider、run config hash、prompt hashes、总体通过率、semantic differences 和 skill trace。
5. 按失败归因选择一个最小优化点：
   - 回答正确但 DocMate 漏命中：先改 DocMate description。
   - DocMate 误命中普通聊天：收紧 description。
   - DocMate 已正确命中但回答错误：优化 DocMate FAQ/引用流程或相关核心逻辑。
   - 输入缺关键消息/资源：修 scenario 或生产上下文构造，不把问题归因到 skill。
6. 每次只改一个归因层，用同一 golden、同一 repeat 和同一运行时模型设置做对照。候选稳定后再用 `repeat=3` 检查波动。

## 完成标准

- 当前 10 个 Task Session case 都有 owner 审核后的 golden label 和 `expected_skills`。
- 生产 Task Session prompt/output schema 没有新增 trace 字段。
- 每个真实 Hermes Task Session trial 有可用或明确失败原因的 eval-only skill trace。
- 报告能区分最终回答失败、DocMate 漏命中、DocMate 误命中和仓库证据不足。
- 至少完成一轮 baseline 与一个单变量优化后的对照评测。
- 所有测试、Ruff 和 whitespace 检查通过；代码和 tracked 文档提交，`data/`、本地 config 与真实运行产物不提交。

## Suggested skills

- `docmate`：回答或核验项目文档、配置、API、版本和故障问题，并按 catalog 查阅 Ubuntu 上的相关仓库。
- `code-review-and-quality`：实现完成、提交前执行多轴代码 review。
- `handoff`：新会话再次中断或需要换 Agent 时更新交接摘要；不要把真实消息、token 或绝对个人信息写入 tracked 文档。
