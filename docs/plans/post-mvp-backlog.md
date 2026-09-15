# Post-MVP Backlog

状态：2026-09-16 起按已确认顺序实施。本文件集中记录当前未完成工作；完成项验收后移除。P1–P21 等历史阶段计划不作为当前 TODO，P22 作为长对话证据方法的引用，本轮均不重写。

## 已确认的范围与决策

- 当前产品内的可靠性、前端体验、运维能力和回答效果优化全部纳入，按依赖与独立交付的顺畅程度推进。
- 保持单 owner、本地助手定位及[当前架构边界](../architecture/current-boundaries.md)。远程 console、多用户、部署安装器等扩展单独决策。
- 前端重组为中文“待我处理”工作台，技术 ID、错误码和必要术语保留原文；聚合发送结果不确定、阻塞/失败、待审批事项，保留专业详情入口。
- 草稿按审批 ID 在当前页面会话内隔离保存，切回时恢复，刷新后不保证保留。提交绑定审批对象及有效版本；过期草稿须重新核对，后台刷新不得覆盖编辑内容。
- 后续会话可只读分析本机运行记录与评测产物；tracked 文档仅保存脱敏结论和可复现方法，真实消息、配置、数据库及原始报告不入库。新消息采集、模型评测调用和真实发送须先明确各自范围，本次确认不包含这些外部动作。
- 本次仅落地计划，不实现代码。以下为下一会话的依赖顺序，每阶段独立验收并提交；实际证据要求超过 10 阶段时报告原因，调整未完成计划，不强行拼接无关工作。

## 检查基线与证据分级

本轮实施前基线：`1dbd4c2`。未拉取远端，未检查线上健康，未操作生产记录。

| 类别 | 当前证据 | 阶段 |
| --- | --- | --- |
| 源码可确认的导航缺陷 | Tasks/Approvals 的选择 effect 会把不在当前列表中的详情链接目标替换成首项；Dispatch 已有 selectedId 保护，可作对照 | S2 |
| 源码可确认的浏览限制 | Tasks/Approvals/Dispatch 固定 limit=50、offset=0，无翻页入口；主要队列没有定时刷新 | S2 |
| 源码可确认的展示偏差 | dashboard_snapshot 默认返回最多 20 条预览，Dashboard 用数组长度展示审批/发送异常数量；顶部状态不体现请求错误和缓存过期 | S2–S3 |
| 已记录、尚未实现 | ingestion._drain 全量分页并累积 items；上限、时间预算、积压指标仍未落地 | S4 |
| 设计风险，尚非线上故障结论 | ADR-0016 的保留 checkpoint 后重拉方案，必须证明固定窗口持续超 cap 时不会反复停在同一批页面 | S4 |
| 已记录、尚未完成 | bot membership 运行时事实、路由冲突契约、后台背景与统一重试 | S5–S8 |
| 待取证的效果假设 | TaskSessionRunner._prompt_message_ids 在 fresh 时使用任务全部消息，resumed 时使用当前消息；长上下文效果仍需对照评测 | S9–S10 |
| 文档/环境漂移 | docs/testing.md 仍描述不升级旧 schema，store/migrate.py 已有旧库迁移；本机工具版本与项目约束不一致 | 执行检查及相关阶段 |

当前使用 `uv 0.12.4` 完成 `uv sync --locked --extra cards`。实施前 Python 全量为 **705 passed, 1 skipped**；S1 指定 Python 契约为 **209 passed**，前端为 **4 passed**，Ruff lint/format、Pyright、前端 typecheck/lint/build 均通过。未运行打包或真实端到端。

## 实施阶段

依赖表示技术前置；编号表示默认实施顺序。每阶段包含必要的 UI/API/命令/存储纵向改动与测试，不能把阶段是否正确推迟到后续证明。PY、FE 检查缩写见文末。

### S2 — 列表与详情可靠浏览

依赖：无（审批操作安全前置已满足）。交付：队列可完整浏览，详情链接稳定，数据新鲜度明确。

- Tasks/Approvals/Dispatch 增加分页，过滤条件变化时回首屏；排序有稳定次级键，明确翻页期间新增/状态变化的刷新行为。
- 选中对象与 URL 对齐；链接目标独立于当前过滤器/页码加载，不被首项覆盖。补任务、审批、发送动作、消息间关联跳转及浏览器前进/后退。
- 区分加载、空列表、会话失效、对象不存在、读取失败和已有数据过期；安全解析异常 hash，避免畸形链接使页面崩溃。
- 队列定时/手动刷新及命令后缓存失效覆盖相关列表和详情，保留草稿；审查 queryKeys 中详情失效遗漏。
- 验收：至少 51 条数据；跨过滤器、跨页详情链接；命令后刷新；断网、会话失效、旧响应迟到；刷新和回退不串对象、不丢编辑内容。
- 检查：FE；PY `tests/test_operator_query.py tests/test_console_api.py`。
- 源码入口：各队列 Screen、`App.tsx`、`queryKeys.ts`、`src/feishu_shadow_agent/operator_query.py`。

### S3 — 中文“待我处理”工作台

依赖：S2。交付：围绕 owner 处理事项组织的完整工作台。

- 在现有 Dashboard 上聚合发送结果不确定、阻塞/失败、待审批事项；同一任务关联展示，保留各对象身份与独立操作入口。
- Operator Query 提供只读聚合和准确总量，不能把截断预览长度当总数；区分审批数、任务数、异常数，避免重复计数。
- 操作前能看清触发消息、建议回复、阻塞原因、发送目标与当前状态；技术 JSON/审计详情按需展开，设置和健康页面保留直接入口。
- 统一中文操作文案，保留必要技术原文；明确更新时间、读取失败及缓存过期。覆盖桌面、窄屏、键盘导航、可见焦点及可访问标签。
- 验收：空待办、混合异常、大量记录、重复关联、加载/失败状态，以及定位对象并完成处理的闭环；后续 S4–S8 新状态随各阶段接入。
- 检查：FE；PY `tests/test_operator_query.py tests/test_console_api.py`；使用合成数据做浏览器视觉和操作验收，不操作生产记录。
- 源码入口：`DashboardScreen.tsx`、`App.tsx`、`styles.css`、`components/Primitives.tsx`、Operator Query。

### S4 — 有界摄取与积压恢复

依赖：无；默认在 S3 后实施，并接入工作台指标。交付：有界且能持续前进的摄取链路。

- 落实每 chat 页数/消息数上限和每 tick 全局时间预算；明确 group search、P2P、active watch 的预算与公平调度，不能把跨 chat 搜索简单当成单 chat 拉取。
- 遵守 [ADR-0016](../adr/0016-ingest-caps-defer-not-truncate.md)：未 drain 完整窗口不能推进“完整成功”checkpoint，不静默截断。
- 先用固定超限窗口证明进度方案；保存可恢复进度或分割窗口时处理排序、分页 token 失效、重启、重叠去重和消息修订。现有 ADR 对进度表示不足时补充当前决策，不能每 tick 重读相同前缀。
- checkpoint 年龄、页数/消息数、drain 完成、预算耗尽、积压与恢复进度经 Operator Query 和 JSONL 可见。
- 验收：固定窗口超过两次 cap 仍最终 drain；持续写入时其他来源仍推进；中途失败、重启、token 失效、重复/修订消息不漏不重发；预算耗尽后后续 daemon 阶段仍可运行。
- 检查：PY `tests/test_p2_ingestion_routing.py tests/test_daemon.py tests/test_config.py tests/test_operator_query.py`；指标 UI 用 FE 验证。
- 源码入口：`ingestion.py`、`daemon.py`、`config.py`、checkpoint 存储与 Operator Query。

### S5 — Bot membership 运行时恢复

依赖：无；默认在 S4 后实施。交付：可信的成员事实、派生降级、通知与恢复可见性。

- 按 [ADR-0015](../adr/0015-bot-membership-derived-at-runtime-not-policy-mutation.md) 扩展发送/下载失败归因，适配 `im chat.members bots` 主动探测；先验证工具链实际能力与返回结构，缺失则记录依赖，不能把 unknown 当 absent。
- 区分确认在群、确认离群、未知/探测失败，定义事实缓存有效期和重试节奏；按 reply_identity、allow_user_fallback 派生 Effective Policy。
- 确认离群时阻断资源下载并按已有身份策略降级回复；通知 owner 去重，恢复入群后重新派生状态。不得自动改写 Policy Store 或自动加群。
- 验收：离群、重加入、超时/权限错误、事实过期、fallback 开关组合；未知不误判，通知不刷屏，运行时变化不产生伪造的 Policy Audit。
- 检查：PY `tests/test_lark_cli.py tests/test_policy_runtime.py tests/test_dispatcher.py tests/test_p2_ingestion_routing.py tests/test_operator_query.py`；FE 验证状态展示。
- 源码入口：`feishu/lark_cli.py`、`policy.py`、`dispatcher.py`、资源下载路径与 Operator Query。

### S6 — 路由边界补齐

依赖：无。交付：既定激活与任务归属语义的完整契约。

- 核对并固化 `reply_to > thread > burst > Router/新任务`；纯 mention 歧义交 Router，换题由新消息/新线程处理。
- incidental mention 不是信号；owner 消息保持 takeover/IGNORE，不引入 keyword 入口或 per-chat activation_mode。
- 补多 active task、冲突关联信号、跨 chat 引用及消息修订测试；已正确逻辑只补缺失契约，不为了覆盖率重写实现。
- 验收：确定性优先级下有唯一可解释归属；跨 chat 引用不触发跨群路由或抓取。
- 检查：PY `tests/test_message_eligibility.py tests/test_p2_ingestion_routing.py tests/test_revision.py`；同步当前产品/路由说明。
- 源码入口：`routing.py`、`message_eligibility.py`、`ingestion.py`。

### S7 — 统一人工重试

依赖：S4–S6，UI 复用 S3。交付：任务处理、资源下载与 dispatch 的统一人工恢复入口。

- CLI/Console 经 Operator Command 按当前阻塞阶段提供操作；仅终态/阻塞态可重试，in-flight 禁止；保持 owner-only 身份和审计。
- 复用业务幂等键及现有 claim 校验机制，每次新 attempt 使用新的 claim token；旧执行者不能回写新 attempt，不能把“复用机制”实现成复用旧 token。
- 已发送、过期 revision、owner 接管、dry-run provenance 门禁继续有效；failed_needs_review 保留人工核实与显式恢复，不加入后台自动重发。
- 验收：资源恢复后继续正确处理、任务失败后重试、重复命令、并发 claim、旧 worker 迟到、修订失效；操作结果及审计能追溯原失败与新 attempt。
- 检查：PY `tests/test_operator_commands.py tests/test_console_api.py tests/test_dispatcher.py tests/test_p3_hermes_approval.py tests/test_revision.py`；FE 验证恢复闭环。
- 源码入口：`operator_commands.py`、`processing.py`、`dispatcher.py`、`store/sqlite_store.py`。

### S8 — Owner 补充任务背景

依赖：S7，UI 复用 S3。交付：显式、可审计的任务背景编辑能力。

- owner Operator Command 保存/修改/清空任务级背景，带审计与版本；仅 fresh 重建时作为任务证据注入，不改变 owner 原聊天消息的接管语义。
- UI 明确“下次 fresh 重建生效”，不暗示已进入当前 live provider session；背景服从任务隔离及 retention 清理。
- 与 P22 的 fresh 输入方向兼容，但本阶段不依赖 running summary；模型可见字段遵守当前架构的目的/生产者/消费者/失败路径/回归测试清单。
- 验收：写入、替换、清空；fresh 可见、resumed 不重复注入；跨任务隔离；保留期后清理；迁移旧库不损坏现有状态。
- 检查：PY `tests/test_operator_commands.py tests/test_console_api.py tests/test_prompt.py tests/test_store_schema.py tests/test_retention.py`；FE 验证编辑与生效提示。
- 源码入口：Operator Command、`task_session_runner.py`、`prompt.py`、store/schema/migrate、retention。

### S9 — 回答质量基线与失败归因

依赖：S6、S8。交付：可复现的评测基线、失败分类与优化准入结论。

- 先只读清点本机运行记录、feedback、capture/golden、报告的版本、覆盖范围与可用性；历史结果不能冒充当前基线。
- 按路由、入口漏收、资源阻塞、证据不足、fresh/resumed 上下文、回复表达分类，区分代码拒绝、模型判断与真实发送结果。
- 真实样本/长对话证据不足时明确缺口；合成用例验证契约，不代替真实效果收益。人工标签及样本 promotion 保留人工确认边界。
- 按 [P22 证据计划](p22-task-session-context-budget-evidence.md) 比较现状、root+最近 N 无摘要、有界窗口+系统组合摘要；固定 backend/model/config/样本，逐项改变变量。
- 记录样本数、误答/漏答及分母、早期事实保留、漂移、输入长度、可获得的耗时/成本与缺失指标；外部模型评测执行前明确范围和预算。
- 验收：样本来源/脱敏方式、配置标识、重跑方法、失败归因和 S10 准入依据可审阅；无样本或未授权评测时标为阻塞，不能宣称基线已完成。
- 检查：按改动选取 PY `tests/test_eval_capture.py tests/test_eval_ingress.py tests/test_eval_model.py tests/test_eval_schemas.py tests/test_prompt.py`；另执行已明确范围的真实评测。原始产物不入库。
- 源码入口：`evals/`、`docs/evals.md`、P22、feedback 查询与 Task Session runner。

### S10 — 证据支持的后端效果优化

依赖：S9。交付：针对已归因问题、经对照验证的优化，或有证据支持的不采用结论。

- 根据 S9 选择上下文、资源证据组织、回复表达等实际有效改动；每次改变一个可归因变量，保留安全、路由、审批回归。
- 长对话方向保持 root+最近 N+可选系统组合摘要，仅 fresh 注入，follow-up 保持当前单条模式；N、摘要内容及是否进入生产 schema/prompt 由评测决定。
- 验收同时报告质量和成本：不能靠沉默或转交更多任务给 owner 来制造“误答减少”，不能把输入缩短直接视作效果改善。
- 收益未证实时保留现状并记录不采用结论；证据缺失则继续阻塞，不能以“无收益”关闭。其他已确认失败项未处理时不得整体关闭效果待办。
- 如归因产生多个独立改动或需要超过本计划的阶段数，报告证据并重排未完成阶段，不把所有效果工作塞入一个不可审阅提交。
- 检查：相关 PY/FE 契约测试、与 S9 相同条件的对照评测、最终完整检查；prompt/schema 改动覆盖字段形状与旧库升级。

## 外部依赖与独立待决策项

| 条目 | 状态及启动条件 | 完成条件 |
| --- | --- | --- |
| 合并转发子资源下载 | 外部依赖：lark-cli 暴露 merge_forward 原始 message_list/子消息 ID；先核验工具链能力 | 使用当前消息可见的子消息标识尽力下载，缺失或失败保留占位符；遵守 ADR-0013，不跨源 chat 抓取 |
| 通用配置编辑和 per-user policy | 独立待决策；当前 Policy/Settings 沿用，任意配置写入不纳入本轮 | 先定义配置变更审批、审计、回滚及用户语义，再单独设计实施 |
| 部署与外部集成扩展 | 独立待决策：LaunchAgent/systemd/Windows service、桌面/远程 console、SDK/OAuth、向量检索及细粒度资源分析 | 明确产品需求后逐项决策，必要时更新当前架构边界；本表不代表实施授权 |

## 执行检查与新会话交接

- **PY `<paths>`**：`uv run --locked pytest -q <paths>`，按阶段覆盖新增行为与实际变更的相邻契约。
- **FE**：`npm --prefix frontend/operator-console run lint`、`npm --prefix frontend/operator-console test`、`npm --prefix frontend/operator-console run build`。build 包含 TypeScript 检查并刷新 console_static，产物随对应阶段检查和提交。
- 每阶段运行 `git diff --check`，Python 改动补 Ruff lint/format 与 Pyright；只暂存该阶段明确路径，检查 staged diff 后提交。
- 最终运行 `uv run --locked pytest -q`、`uv run --locked ruff check .`、`uv run --locked ruff format --check .`、`uv run --locked --extra cards pyright` 及 FE。发布/打包改动追加 `uv run --locked python -m build` 和 wheel 静态资源验证。
- 用 `uv 0.12.4` 与 `uv sync --locked --extra cards` 校准环境，先复核既有格式问题；修正文档中与现行迁移能力冲突的说明，历史计划不重写。
