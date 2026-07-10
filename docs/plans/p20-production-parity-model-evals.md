# P20 Production-Parity Model Evaluation

## 1. 目标

建立一套本地、可重复、默认无发送副作用的评测工具，用真实飞书消息分别评估并优化：

1. 群消息 Message Acquisition / Message Eligibility。
2. task router 的任务归属决策。
3. task session 首轮和续轮的模型回答。
4. ingress 之后 router 到 task session 的完整处理链路。

评测的首要约束是生产输入一致性。`production_v1` 评测必须调用生产使用的消息标准化、Message Eligibility、路由、task session prompt 构造和状态机；trace 只能记录输入和结果，不得向模型额外注入生产环境看不到的上下文。

## 2. 核心原则

### 2.1 Capture once, replay many

飞书只负责生成本地 capture 或 ingress snapshot。`eval run-*` 默认读取本地 artifact，可针对同一批数据重复运行不同代码和模型版本。只有显式 live 命令允许再次访问飞书。

### 2.2 Production-input parity

- router eval 只运行生产 router，不运行 task session。
- task-session eval 只运行生产 `TaskSessionRunner`，不通过 router 重建任务归属。
- initial 模式没有 session id，模型收到生产首轮会收到的完整 task messages。
- resume 模式先真实运行 setup turn 得到当前 backend 的 session id，再只把 target 当前消息交给生产 resume 路径。
- full-chain eval 才运行 eligibility 之后的 `process_raw_message` -> router -> task session，并记录 would-send 结果；Message Acquisition/Eligibility 仍由 ingress eval 独立评估。
- capture 的邻近消息用于人工选 case 和构造明确标注的任务消息；不能因为存在于 `messages` 表就默认进入 task session prompt。

### 2.3 Draft 与 golden 分离

`label_status` 只有三种状态：

- `none`：没有标签，只有运行观测结果。
- `draft`：模型生成或人工尚未确认的 review 标签，可输出 provisional metrics，但 `passed: null`，不因 mismatch 返回失败。
- `golden`：由人工执行 `promote` 后形成的回归真值，严格评分。

不增加 `human_reviewed`、`review_complete`、`confirmed` 等中间状态。执行 `promote` 本身即代表人工确认。

状态由 artifact 结构判断，不保存额外状态字段：当前 runner type 存在 `*.review.yaml` 时是 `draft`；存在 `eval_case.yaml + labels.yaml + provenance.yaml` 时是 `golden`；只有 `eval_case.yaml` 时是 `none`。混合或缺失必需文件是 artifact 错误。

### 2.4 简洁 schema

只保留评分或定位问题必需的字段。能从输入、结果或其他字段推导的信息不要求模型重复输出。不引入 variant registry、criteria map、confidence、compare 命令、suite manifest 或自定义 metadata 参数。

## 3. CLI

统一挂在现有入口下：

```bash
python -m feishu_shadow_agent eval capture ...
python -m feishu_shadow_agent eval run-ingress ...
python -m feishu_shadow_agent eval run-router ...
python -m feishu_shadow_agent eval run-task-session ...
python -m feishu_shadow_agent eval run-full-chain ...
python -m feishu_shadow_agent eval promote ...
```

runner 单 case 使用 `--case`，批量运行使用 `--cases`，二者必须且只能提供一个。`--cases` 只扫描目录下一层与当前 runner type 匹配的子目录：captured draft 识别对应 `<type>.review.yaml`，普通/golden case 识别 `eval_case.yaml` 并验证其 `case_type`。

`--label` 只作为 run id 的可读后缀并记录为 `run_label`，字符限制为 `[A-Za-z0-9._-]`。不支持 `--metadata`。

Run id 使用 `<type>-<UTC microseconds>-<random suffix>[-<label>]`，创建目录时必须使用 exclusive mkdir；极端碰撞时重新生成，绝不复用或覆盖已有 run 目录。墙上时间只用于 artifact identity，不进入 Evaluation Clock。

router、task-session 和 full-chain 支持 `--repeat N`，默认为 `1`。每个 Evaluation Trial 都必须重建 Temporary Eval Store 并新建 provider session，不能把上一次的任务、session id 或 judge 结果带入下一次。确定性 ingress runner 不接受 `--repeat`。用于比较的批次必须使用相同 repeat 次数。

router、task-session、full-chain 和 judge 默认调用 config 选择的真实 agent backend，并完整遵循 Evaluation Run Config 的 `tool_permissions`，包括 `full_access`。Eval 不自动降级权限、不增加确认 flag、不因 full access 限制 repeat，也不创建临时工作区。`--dry-run-backend` 只用于验证输入构造，不代表模型能力结果。真实飞书和真实模型调用不进入默认 pytest。

`tool_permissions: full_access` 时，Agent 可能修改文件、执行命令或访问外部系统，重复 trial 也可能重复这些影响。CLI 在 full access 与 `--repeat > 1` 组合时输出非阻断警告，report 记录实际 tool-permission profile。安全保证仅限 Python orchestration 不运行 Dispatcher、不发送飞书回复或 owner notification，不承诺拦截 Agent 工具副作用。

退出码：

- `0`：命令成功；golden case 全部匹配。
- `1`：golden case 存在评分 mismatch。
- `2`：参数、schema、artifact 或运行时错误。

draft mismatch 始终返回 `0`，报告中的 `passed` 必须为 `null`。

## 4. Artifact 约定

所有数据位于已忽略的 `data/evals/`，不得提交。可执行 scenario 要么来自 captured `<type>.review.yaml` 的 `scenario`，要么来自普通/golden `eval_case.yaml`。runner 根据 artifact 结构和 schema 判断语义，不根据路径名是否包含 `golden` 判断。

Golden case 中，`eval_case.yaml` 是 Evaluation Scenario：只包含执行所需的 message ids、task fixtures、mode 和 target。`labels.yaml` 只包含 expected route/answerability、reference answer 等 ground truth。Captured router/task-session/full-chain 的 `*.review.yaml` 是人工 authoring 文件，可以为编辑便利组合 `scenario` 和 `labels`；`promote` 必须分别校验并拆分，不能把 review template 原样当成 golden label。Ingress 的 `labels.review.yaml` 只包含逐消息 labels，其 scenario 由 ingress run 目录提供。

### 4.1 Metadata

```yaml
schema_version: eval_metadata_v1
created_at: "..."
git_commit: "..."
git_dirty: true
config_hash: "..."
config_base_dir: "/original/config/directory"
prompt_hashes:
  router: "..."
  task_session: "..."
  semantic_judge: "..."
agent_backend:
  backend: hermes | codex | claude_code
  model: "..." | null
  model_provider: "..." | null
lark_cli_version: "..."
contains_private_data: true
config_contains_sensitive_fields: false
```

captured/golden artifact 中的 `config.yaml` 是作者生成该 case 时的 Case Baseline Config。Runner 仅使用命令行 `--config` 选择的 Evaluation Run Config 执行 backend、prompt、lifecycle 和 policy，不合并两份配置，也不隐式使用 case 内配置。每个新 run 目录都复制本次实际 `--config`，并在 metadata 保存原 `config_base_dir`，避免副本中的相对路径改为相对 artifact 目录解析；仅当 config hash 未被编辑时才恢复该 base dir。Report 记录 `case_config_hash`、`run_config_hash` 和 `config_changed`。两者不同允许运行；要精确重跑基线时显式传入 `<case>/config.yaml`。

Case 与 run 的 `owner.open_id` 必须一致，因为它会改变 normalization、sender role 和 mention 判断；不一致时以 artifact/config error 退出。每次复制 config 前都扫描敏感字段。默认发现敏感字段即失败；仅显式 `--allow-sensitive-config` 可以继续，不自动脱敏。metadata 只保存 prompt hash 和 git commit，不复制完整 prompt。

`agent_backend` 记录一套实际 backend/model 身份，不重复拆成 router/task-session model，因为两者使用同一 Evaluation Run Config。`prompt_hashes` 是动态 map，只记录本 artifact/run 实际调用过的 prompt type；除 `router`、`task_session` 外，可包含 `ingress_judge`、`semantic_judge` 和 `reply_postprocess`，不用 null 占位。

### 4.2 Evaluation Clock

不在 `eval_case.yaml` 增加 `evaluation_at`。每一轮以当前 scenario message 的 `sent_at` 作为逻辑时间；full-chain setup 按显式 message id 顺序运行，并在每一轮使用该消息自己的时间。Golden promotion 必须拒绝缺失、不可解析或晚于 target 的 setup 时间，不得回退到机器当前时间。metadata `created_at` 和 run id 的墙上时间只描述执行，不得影响 lifecycle、模型输入或评分。

### 4.3 Evaluation Resource Fixtures

router/task-session/full-chain scenario 中被引用的消息如果包含图片或文件，`eval_case.yaml` 必须显式声明成功捕获的 resource fixture：

```yaml
resources:
  - message_id: om_1
    file_key: file_xxx
    resource_type: file
    sha256: "..."
```

artifact 内的文件路径固定从 `message_id + resource_type + file_key hash` 推导，不在 schema 里增加 `path`。Capture 使用现有生产资源下载能力将字节保存到 case 的 `resources/`；promotion 校验 raw message resource reference、文件存在性和 SHA-256。每个 Evaluation Trial 将 fixture 复制到自己的临时 resource directory，不共享可变路径。

task-session runner 使用 fixture 在 Temporary Eval Store 中重建生产格式的 `downloaded` resource row。full-chain 通过仅能读取 case fixture 的 eval Feishu client 向生产 `ResourceProcessor` 提供字节，不跳过正式处理逻辑，也不访问飞书网络。捕获失败或不支持的 resource case 可保留为 draft，但不得 promote。`bot_not_joined`、下载失败、文件过大和配额等资源异常策略不属于本次模型能力 eval。

### 4.4 Provenance

只有 golden case 包含 `provenance.yaml`：

```yaml
schema_version: eval_provenance_v1
promoted_at: "..."
source:
  kind: capture | ingress_run
  case_id: "..."
  run_id: "..."
review_source: <type>.review.yaml
promoted_by: local_user
```

golden 必须复制运行所需的最小完整 artifact，不能依赖 captured/run 原目录继续存在，也不得包含 production store snapshot：

- ingress：`raw_messages.jsonl`、带 acquisition sources 的 `ingress_timeline.yaml`、`config.yaml`、`metadata.yaml`；不复制 store snapshot。
- router/task-session/full-chain：只含 scenario 引用消息的 `messages.jsonl`、`eval_case.yaml`、`labels.yaml`、`config.yaml`、`metadata.yaml` 和这些消息引用的 `resources/` 文件。

报告统一写入：

```text
data/evals/runs/<type>/<run_id>/
  config.yaml
  metadata.yaml
  report.yaml
  trials/
    001/
      report.yaml
      events.jsonl
      prompts/              # 仅 debug.save_full_agent_io=true
```

批量运行额外生成 `summary.yaml` 和 `cases/<case_id>/report.yaml`，不能把每个子 case 散落成同级独立 run。

每个 Evaluation Trial 在唯一私有临时目录中从空 SQLite 迁移开始，按 scenario 显式顺序重建 messages、tasks、watch keys 和 resources，所有会影响查询、prompt 或状态机的业务时间都使用 Evaluation Clock，不使用墙上时间。为避免随机目录进入 production `context_access.read_only_uri` 和 resource path，同一 case 通过互斥锁使用稳定的 `.trial-slots/<case-hash>/current` 访问别名；别名只指向当前 trial 的私有目录，同一 case 的并发运行串行化。固定 scenario 的插入顺序、临时 row IDs、DB 中的路径和模型 prompt 必须稳定，后续 trial/run 永远不能打开之前的 DB。

Trial Evidence Bundle 保留 `report.yaml` 和 `events.jsonl`；只有 Evaluation Run Config 启用 `debug.save_full_agent_io` 时才保存完整 prompts。Report 必须物化 Router candidates/decision、task alias map、task-session plan、原始模型 JSON、raw/effective reply、状态转移和 would-send trace。证据写入后在 success/failure 两条路径都删除稳定访问别名、Temporary Eval Store 和 trial-local resource copies；进程崩溃遗留的临时目录不得被后续运行复用，下一次持锁运行会替换遗留别名而不是打开旧 DB。

这一边界保证 eval-owned DB 基线隔离和可重建性，不保证模型输出逐字节一致，也不保证 `tool_permissions: full_access` 时 Agent 外部工具副作用的幂等性。

repeat report 保留每个 trial 的完整结果，并聚合 `passed_trials`、`failed_trials`、`error_trials`、`pass_rate` 和 semantic difference type 计数。不计算平均分。case 只有在所有 trials 均通过时才是 `passed: true`；调用/schema/状态错误进入 `error_trials`，结构或语义不通过进入 `failed_trials`。

Batch 在单个 case 的 artifact/schema 或 trial runtime error 后继续其他 case，保留全部 report 并最终以退出码 `2` 结束。只有顶层 Evaluation Run Config、CLI 参数或 output root 无法创建这类全局错误才在扫描 case 前立即中止。Case preflight error 不重复 N 次；已进入 trial 的 backend/judge runtime error 不阻断同 case 后续 trials。

所有 model runner report 都显示 Case Baseline Config 和 Evaluation Run Config 的 hash/model 差异，但不提供自动 compare 或归因结论。

## 5. Capture

不提供 `discover`、`list-candidates` 或 `latest` 命令。一个 `capture` 同时承担候选列表和按 message id 落盘。

只列候选，不写文件：

```bash
python -m feishu_shadow_agent eval capture \
  --config config.yaml \
  --lookback-days 2 \
  --limit 20
```

候选来自最近 @owner 的群消息和最近私聊消息，调用 `lark-cli +messages-search`。

Capture 在同一固定 start/end 窗口内分别执行 `--is-at-me` 和 `--chat-type p2p` 搜索，先获取并去重两类结果，再按 Lark create time 倒序统一应用 `--limit`；`--limit` 不分别限制两个 source。同一 message 同时出现时只列一次，但保留全部 candidate sources。

选择 seed 后落盘：

```bash
python -m feishu_shadow_agent eval capture \
  --config config.yaml \
  --message-id om_xxx \
  --context-before 20 \
  --context-after 0
```

`--message-id` 直接通过 `+messages-mget` 获取 seed，不依赖上一次候选列表或本地隐式状态。P2P seed 的 draft source 为 `p2p`，group direct-mention seed 为 `group_at_me`；其他显式 message-id group seed 可在 authoring review 中建议为 `active_watch`，runner 仍只读取 review/golden 中的显式 source，不重新推断。

`--context-before` 默认 20；`--context-after` 默认 0。after 是显式调试选项，因为它会引入 seed 之后的未来消息。上下文通过 seed 所在 chat 的 `+chat-messages-list` 前后窗口获取；每个方向先请求至少 `N + 1` 条，排除 seed 并按 `message_id` 去重后不足 N 条时继续翻页，直到满足数量或到达窗口边界。最终按 Lark 时序稳定排序，避免为了少量邻近消息拉取无界群历史。

```text
data/evals/captured/<case_id>/
  messages.jsonl
  resources/                 # 仅在捕获到引用资源时存在
  config.yaml
  metadata.yaml
  router.review.yaml
  task_session.review.yaml
  full_chain.review.yaml
  REVIEW.md
```

router/task-session/full-chain 的 review 文件固定分为两个顶层对象，不混入人工审核状态：

```yaml
schema_version: task_session_review_v1
scenario:
  case_type: task-session
  mode: initial
  message_ids: [om_xxx]
labels:
  reference_answer: ""
  answerability: null
  watch_action: null
```

captured 目录本身是可运行 draft：`run-router`、`run-task-session`、`run-full-chain` 分别读取同目录的对应 review 文件，不需要先 promote。三个 review 的 scenario 互相独立，不再用一个根 `eval_case.yaml` 表示多种 case type。

Review 的 `scenario` 必须在 capture 后就是可执行的严格输入；`labels` 允许 `null`/空字符串占位，以便未人工填写时仍能运行完整候选链路。Draft runner 只对已填写字段输出 provisional mismatch，不使用空 reference 调用 semantic judge，且 `passed` 始终为 null。Promotion 使用严格 golden schema 拒绝任何占位值。

`messages.jsonl` 是去重后的 raw message pool，每行直接保存一条未经修改的 Lark message，不使用 `{role, raw}` wrapper，也不单独保存 `raw_seed.json`。Captured case 可以包含完整前后窗口和建议的 candidate task messages；message 的 target/setup/task-fixture 角色只由 review template 和 golden `eval_case.yaml` 引用决定。

Promotion 必须验证 scenario 引用的 message id 全部存在，并只把被引用的消息及其已校验的 resource fixtures 复制到 golden；未引用的前后窗口不得进入 golden 或模型可见的 Temporary Eval Store。

Capture 可以只读查询 production store 以建议相关 task fixtures 和补齐被显式选择的 raw messages，但不得复制 production DB。未被 Evaluation Scenario 引用的 message、task、approval、action、audit 和 session state 不进入 artifact，也不进入 Temporary Eval Store。Production `ContextAccessBuilder` 仍按正式规则工作，但只能看到 scenario 明确包含的数据。

## 6. Message Eligibility Eval

### 6.1 边界

v1 只评估群消息，不评估 P2P。Message Acquisition 负责通过 source-specific Lark 查询和 chat/thread window 提供 raw messages；Message Eligibility 在 normalization 之后、task routing 之前作出确定性判断。

`kept` 表示已 acquired 的消息有资格进入 task ownership 和 handling；`dropped` 表示消息未被 acquisition 获取，或在选择目标 task 之前被确定性过滤。Eligibility 必须是无 DB、source-aware 的纯策略，只接收 normalized message 和 acquisition sources：保留 direct mention、有效 active-watch follow-up 和 owner intervention，过滤 self-loop、`@All` 和无关噪音，但不得读取 task/store 状态或选择目标 task。生产 daemon 与 eval 必须调用同一个 Message Eligibility policy。

Message Acquisition 的实际返回集合需要独立记录，包含 `group_at_me` / `active_watch` 等 source；active task 和 watch-key 匹配完全属于 Acquisition。不能用 Eligibility 结果反推 Lark 查询行为，router、task session 的结果也不能冒充 Eligibility。

### 6.2 Live 与 snapshot

live 必须显式提供 `--chat-id`，使用 `lark-cli +chat-messages-list --order asc` 获取同群完整时间窗，而不是只拉 `is_at_me=true` 的结果：

```bash
python -m feishu_shadow_agent eval run-ingress \
  --config config.yaml \
  --chat-id oc_xxx \
  --lookback-days 2

python -m feishu_shadow_agent eval run-ingress \
  --config config.yaml \
  --chat-id oc_xxx \
  --start 2026-07-10T09:00:00+08:00 \
  --end 2026-07-10T11:00:00+08:00
```

固定 snapshot 可重复运行：

```bash
python -m feishu_shadow_agent eval run-ingress \
  --config config.yaml \
  --snapshot data/evals/ingress-runs/<run_id>
```

`--snapshot` 必须指向完整 ingress run 目录，因为 offline replay 同时需要 `raw_messages.jsonl`、timeline 中冻结的 Lark acquisition sources 和 `eval_case.yaml` 中的本地 Acquisition Scenario。snapshot 模式不得访问飞书或 production DB。live/snapshot 即使 judge 失败也要保留新 run 目录，不能修改输入目录。

Live run 同时获取完整 chat timeline 和可重放的 Message Acquisition 输入。Lark-owned `group_at_me` source 是该窗口的服务端观测结果，offline replay 保持冻结；本地 `active_watch` source 则根据执行 live run 时仍活跃、并物化到 `eval_case.yaml` 的 task/watch-key fixtures 重新计算，然后运行当前 Message Eligibility policy：

```yaml
# eval_case.yaml
schema_version: eval_case_v1
case_type: ingress
acquisition:
  active_tasks:
    task_1:
      chat_id: oc_xxx
      thread_id: null
      watch_keys:
        - user:ou_xxx
        - msg:om_root
```

这不是历史 daemon acquisition 日志：当前 store 不保存完整 task 状态变更时间线，因此对于早于 live run 的窗口，它不会还原后来已关闭或过期的 task，也不会证明某条消息当时实际被 daemon 拉取。它回答的是“冻结服务端 group-at 观测，并按 capture-time active-watch baseline 运行当前本地策略”的反事实问题。需要历史实际 acquisition 证据时必须在目标窗口运行 daemon 并保留日志，不能从该 snapshot 反推。修改 Lark query 或服务端 `group_at_me` 行为后必须重新 live capture；修改本地 active-watch matching 或 Message Eligibility 时可以重放固定 snapshot。

### 6.3 Timeline 与 review labels

```text
data/evals/ingress-runs/<run_id>/
  eval_case.yaml
  raw_messages.jsonl
  ingress_timeline.yaml
  labels.review.yaml
  REVIEW.md
  config.yaml
  metadata.yaml
```

`raw_messages.jsonl` 保存可重放的 lark-cli 原始输出。`ingress_timeline.yaml` 是唯一的 normalized timeline 和 judge input，保存同群同窗完整消息、实际 Message Acquisition sources 和生产 Eligibility decision trace，不另建 acquisition trace 文件：

```yaml
schema_version: ingress_timeline_v1
instruction:
  task: judge_ingress_filter
  production_ingress_contract: production_v1
owner:
  open_id: ou_xxx
  name: Owner
chat:
  chat_id: oc_xxx
  chat_name: "..."
  start: "..."
  end: "..."
messages:
  - index: 1
    message_id: om_xxx
    sent_at: "..."
    sender_role: external_user_message
    sender_id: ou_xxx
    sender_name: User
    text: "..."
    mentions_owner: false
    at_all: false
    reply_to_message_id: null
    thread_id: null
    sources: [group_at_me, active_watch]
    current_decision: kept | dropped
    reason_code: not_acquired | self_message | at_all_suppressed | owner_intervention | direct_owner_mention | active_watch_message | non_direct_mention
```

`sources` 是本次 eval 的 Message Acquisition input trace，可为空或同时包含多个 source；其中 `group_at_me` 是冻结的服务端观测，`active_watch` 是 capture-time fixture 的本地重算结果。最终 `current_decision` 是 Acquisition 与 Eligibility 的组合：没有 source 时为 `dropped / not_acquired`；存在 source 时运行纯 Eligibility policy。`current_decision` / `expected_decision` 是 artifact 中统一使用的简洁字段。`Message Eligibility` 只作为领域概念和代码类型，不增加 `current_eligibility` 字段。

`reason_code` 按以下互斥优先级计算：无 sources 是 `not_acquired`；bot/agent 自身消息是 `self_message`；`@All` 是 `at_all_suppressed`；owner 介入是 `owner_intervention`；`group_at_me` direct mention 是 `direct_owner_mention`；包含 `active_watch` 是 `active_watch_message`；只有 `group_at_me` 但不是 direct mention 是 `non_direct_mention`。多个 sources 仍只输出一个 reason。不增加可由它推导的自然语言 `reason`，也不使用 `unknown` 兜底；未知 source、sender role 或未覆盖组合是 schema/runtime error。

judge 输入是同群同窗完整 timeline 和 current Eligibility decision。它使用 Evaluation Run Config 的 `agent_backend`，必须以 `session_id = null` 进行隔离单轮调用。prompt 必须明确要求独立判断，不机械接受当前代码结果，也不得判断消息属于哪个 task。judge 直接生成覆盖每一条扫描消息的 `labels.review.yaml`，不额外生成 `judge_output.yaml`：

```yaml
schema_version: ingress_review_labels_v1
source_run: ingress-20260710-100000
labels:
  - message_id: om_xxx
    timeline_index: 42
    sent_at: "..."
    sender_name: User
    text_excerpt: "..."
    current_decision: dropped
    reason_code: not_acquired
    expected_decision: kept
    review_reason: "这是对 owner-directed task 的上下文追问"
```

人工直接修改 `expected_decision` 和 `review_reason`。一致项 `review_reason` 留空；不一致项必须给出非空原因。排序先放不一致项，再按 timeline 顺序放一致项。

judge 失败时仍为每条消息生成 fallback label：`expected_decision = current_decision`、`review_reason = ""`，并向用户警告。`REVIEW.md` 和 YAML 顶部注释说明审核规则及 promote 命令。

### 6.4 Promote 与 golden replay

```bash
python -m feishu_shadow_agent eval promote \
  --config config.yaml \
  --type ingress \
  --run data/evals/ingress-runs/<run_id> \
  --review data/evals/ingress-runs/<run_id>/labels.review.yaml \
  --name <case-name>
```

promote 必须验证：timeline 每条消息恰好一个 label、无重复或缺失、decision 只能是 `kept|dropped`、不一致项原因非空、message id 全部存在。

golden `labels.yaml` 只保留：

```yaml
schema_version: ingress_golden_labels_v1
labels:
  - message_id: om_xxx
    expected_decision: kept
    review_reason: "..."
```

回归运行不访问飞书、不调用 judge：

```bash
python -m feishu_shadow_agent eval run-ingress \
  --config config.yaml \
  --golden data/evals/golden/ingress/<case-name>
```

报告给出 total、expected/actual kept、TP/TN/FP/FN、precision、recall、passed，以及每条 mismatch 的 expected、actual、error type、当前 reason code 和人工 review reason。

## 7. Router Eval

router eval 只运行 production router；确定性决策直接评分，生产路径需要模型 router 时才调用真实 backend，不调用 judge agent，也不复用历史 audit 代替本次执行。

```yaml
# eval_case.yaml
schema_version: eval_case_v1
case_type: router
target:
  message_id: om_xxx
  source: group_at_me | active_watch | p2p
tasks:
  task_1:
    status: watching | closed | closed_by_owner | human_taken_over
    task_label: "..." | null
    message_ids: [om_1, om_2]
```

```yaml
# labels.yaml
schema_version: router_labels_v1
route: new_task | attach_task | reopen_task | ignore | ambiguous | human_taken_over
task_key: task_1  # 仅 attach/reopen/human_taken_over
```

`task_key` 在 `attach_task`、`reopen_task` 或确定性 `human_taken_over` 时必填，`new_task`、`ignore` 和 `ambiguous` 必须省略。scenario 中的 map key 是稳定 Evaluation Task Alias；runner 在 Temporary Eval Store 中重建 task 后维护 alias 到临时 task id 的映射，不依赖 production task id。

Evaluation Task Fixture 必须显式提供生产 `status`、`task_label` 和按 `sent_at` 递增的非空 `message_ids`；`task_label` 可为 `null`，但不得省略，因为它直接进入 Router prompt。chat/thread/root message、watch keys、last user message 和 message count 从这些 raw messages 通过生产 store API 重建。`watch_until` 从最后一条 task message 的 `sent_at` 加实际 config watch window 推导；`status: watching` 在 target Evaluation Clock 必须仍有效，否则 promotion 拒绝。非 watching 状态的 `updated_at/closed_at` 使用最后一条 task message 的 `sent_at` 重建，并由生产 closed-recall 查询决定是否成为候选。Scenario 不再存储这些可推导字段。

`target.source` 是调用生产 Router 时必需的 ingestion source，必须显式为 `group_at_me | active_watch | p2p`。Raw message 的 chat type 和 mention 形状不能唯一推导它，runner 不得自行猜测。

runner 必须精确比较生产 route，并在 attach/reopen/human-taken-over 时比较 task identity。Golden label 不保存不参与判定的 reason、score、模型解释或候选列表；报告保留这些实际运行信息用于失败定位。

## 8. Task Session Eval

task-session eval 不运行 router。它从 case 明确列出的 message ids 在 per-case Temporary Eval Store 中构造 task membership，然后调用 production `TaskSessionRunner`。临时 store 使用正式 schema 和 store API，但不复制或修改 production store；如有 Evaluation Resource Fixtures，runner 在调用前将它们复制到 trial resource directory 并写入 `downloaded` resource rows。

### 8.1 Initial

```yaml
# eval_case.yaml
schema_version: eval_case_v1
case_type: task-session
mode: initial
message_ids: [om_1, om_2]
```

```yaml
# labels.yaml
schema_version: task_session_labels_v1
reference_answer: |
  针对这组首轮 task messages 的标准答案。
answerability: auto_reply | needs_owner | no_reply
watch_action: keep_watching | close
```

没有 session id；`message_ids` 是按 `sent_at` 递增的完整首轮 task message set，列表最后一条是本轮 `current_message_id`。Production runner 从 `task_messages` 读取列表全部消息构造首轮 prompt，reference answer 基于这个完整上下文。

### 8.2 Resume

```yaml
# eval_case.yaml
schema_version: eval_case_v1
case_type: task-session
mode: resume
setup_message_ids: [om_1, om_2]
target_message_id: om_3
```

```yaml
# labels.yaml
schema_version: task_session_labels_v1
reference_answer: |
  用户补充说明后，针对 om_3 这一轮的标准答案。
answerability: auto_reply | needs_owner | no_reply
watch_action: keep_watching | close
```

runner 先将 `setup_message_ids` 作为一个真实首轮 task-session turn 运行，列表最后一条是 setup 的 `current_message_id`；取得当前 provider 的真实 session id 后再 attach 单条 `target_message_id`，并按生产 resume 规则仅发送该当前 target message。setup 输出只记录、不评分；只评分 target turn。不得伪造或跨运行复用 provider session id。Promotion 要求 setup/message 列表非空、ID 无重复且 `sent_at` 严格递增，resume target 必须晚于全部 setup messages。报告记录每轮实际 `task_message_ids`、`prompt_message_ids` 和 `current_message_id`。

### 8.3 结构评分与语义 judge

程序先验证：

- label 和模型 output schema 合法。
- `answerability` 与模型输出的 `auto_reply | needs_owner | no_reply` 精确一致。
- `watch_action` 一致。
- reply 的空/非空状态与 answerability 一致。
- 非 `no_reply` 时 `reply_target_message_id` 必须属于本次 production plan 的 `reply_target_message_ids`，`no_reply` 时必须为 null。
- initial output 的 `task_label` 去空白后必须非空且满足生产长度约束；report 记录实际值，但 golden 不保存 reference task label，judge 不评价标题语义。
- 不含禁止的 mention。

结构失败直接 fail，不运行语义 judge。

语义 judge 与候选回答都使用本次运行的实际 `config.yaml` 中的 `agent_backend`，不增加 eval-only backend 配置。Judge 必须以 `session_id = null` 发起完全隔离的单轮调用，不得继承候选 task session 的 provider session 或隐式历史。Task-session eval 的 candidate 固定为 `TaskSessionRunner` 原始 `proposed_reply`，不运行 reply postprocess、policy gate 或 action 构造。Judge 对照 `reference_answer` 判断事实一致性，不评分语气；文本中的未执行外部动作声称或越权承诺也由 judge 作为 `unsupported_addition` 或 `overcommitment` 判断，程序不做关键词/正则检测。initial judge 同时看到 task messages；resume judge 看到 setup messages、setup model reply、target message、candidate reply 和 reference answer。不得给 judge 生产 task session 本身不可见的整个飞书时间线。

候选回答与 judge 共用 backend 配置意味着：只有 `run_config_hash` 和 backend model 一致的运行才能直接比较 judge 结果。若修改 `agent_backend` 本身，候选模型和 judge 会同时变化，报告必须显示该差异，不得将结果变化单独归因于候选回答能力。

judge 输出固定为：

```yaml
verdict: pass | partial | fail
differences:
  - type: omission | unsupported_addition | contradiction | overcommitment
    severity: minor | major | critical
    summary: "..."
```

只有 `pass` 计为通过，`partial` 和 `fail` 都不通过。`pass` 时 `differences` 必须为空；`partial` 或 `fail` 时必须至少有一条 difference。不增加 score、reason、confidence、criterion results、evidence、blocking issues 或 matched facts。judge 输出必须做严格 schema 校验；调用失败或无效 schema 是运行错误，不得伪装成语义 `fail`，也不能静默当成 `pass`。

Judge prompt 固定 verdict 语义：`partial` 表示候选保留了可用的正确核心，但存在实质性 omission 或附加错误；`fail` 表示核心答案缺失、与必需事实矛盾，或存在 critical unsupported addition/overcommitment。任何 `critical` difference 必须对应 `fail`，`partial` 不得包含 critical difference。

Golden label 中 `answerability` 为 `auto_reply` 或 `needs_owner` 时，`reference_answer` 必须非空，结构通过后运行 semantic judge。`answerability: no_reply` 时必须省略 `reference_answer`，候选 `proposed_reply` 必须为空，不运行 semantic judge。Draft 可以临时保留空 `reference_answer`，但只运行候选模型和可执行的结构检查，语义状态为 `not_scored`且 case `passed: null`。

## 9. Full-Chain Eval

full-chain 从空的 Temporary Eval Store 和显式 scenario 运行生产 `process_eligible_raw_message` -> router -> task session。该入口表示 Message Acquisition/Eligibility 已完成，后续 normalization、持久化、资源、Router 和 Task Session 仍与正式链路共用；ingress 由独立 eval 评分。case 中要求作为前序输入的消息必须按时间顺序真实经过链路；target 是唯一评分轮。不能直接复用已经处理过 target 的 production snapshot，否则 duplicate-message、task aggregate 和 provider session 的未来状态会污染结果。

setup messages 严格按 `setup` 列表顺序逐条使用其显式 `source` 运行完整链路，target 也使用自己的 `target.source`。source 必须为 `group_at_me | active_watch | p2p`，runner 不得从 raw message 猜测。setup 中每个新建任务按创建顺序获得 Evaluation Task Alias `task_1`、`task_2`；后续 setup/target message 路由到已有任务时沿用该 alias。setup 的 router 结果、task-session 回答和状态转移只记录 trace，不另外增加 labels 或评分。setup 调用失败、非法输出或状态写入失败时，case 必须以运行错误中止，不得继续 target。setup 产生合法但与历史不同的任务划分时继续运行，影响由 target 评分体现。

不运行 `Dispatcher`，不真实发送消息或 owner notification；报告记录 would-send actions。离线 replay 的 eval Feishu client 只能从 trial-local Evaluation Resource Fixtures 复制字节，任何飞书 API 访问都必须失败。

```yaml
# eval_case.yaml
schema_version: eval_case_v1
case_type: full-chain
setup:
  - message_id: om_1
    source: group_at_me
  - message_id: om_2
    source: active_watch
target:
  message_id: om_xxx
  source: active_watch
```

```yaml
# labels.yaml
schema_version: full_chain_labels_v1
router:
  route: new_task | attach_task | reopen_task | ignore | ambiguous | human_taken_over
  task_key: task_1  # 仅 attach/reopen/human_taken_over
task_session:
  answerability: auto_reply | needs_owner | no_reply
  watch_action: keep_watching | close
reference_answer: |
  最终有效回复的标准答案。
```

`task_session` 是否应运行可由 router route 完全推导，label 不保存 `should_run`。Router 预期为 `ignore`、`ambiguous` 或 `human_taken_over` 时，必须省略整个 `task_session` 和 `reference_answer`。`new_task`、`attach_task` 或 `reopen_task` 时必须提供 `task_session`，并使用生产原生 `answerability` 和 `watch_action` 枚举。`answerability: no_reply` 时省略 `reference_answer`；其他 answerability 在 golden 中必须提供非空 `reference_answer`。

Full-chain 使用实际 Evaluation Run Config 执行 reply postprocess、composer、policy gate 和 approval/action 构造。Semantic judge 不对照原始 `proposed_reply`，而是对照最终进入 send action 或 approval 的 `final_reply`（report 统一称为 `effective_reply`）。Report 同时保留 `task_session_output`、`raw_proposed_reply`、`effective_reply`、postprocess/gate 结果和 would-send/approval trace；结构评分仍精确判断原始 task-session `answerability` 和 `watch_action`。Postprocess 引入事实错误时，task-session eval 可以通过而 full-chain eval 失败。

Full-chain 先对 target Router route 和必要的 task alias 评分；任一不匹配时 case 直接为结构失败，不对错误任务上下文产生的回答运行 semantic judge。Policy gate 最终选择 send action、approval、watch-only 或 owner notification 时，其类型和 reason 只进入 report/would-send trace，不进入模型能力 golden 评分；本 eval 不代替 policy/dispatch 测试。

ingress 不纳入 full-chain label；它由独立 ingress case 评估。

## 10. Promote

统一命令：

```bash
python -m feishu_shadow_agent eval promote --config config.yaml --type ingress --run <run> --review <review> --name <name>
python -m feishu_shadow_agent eval promote --config config.yaml --type router --case <case> --review <review> --name <name>
python -m feishu_shadow_agent eval promote --config config.yaml --type task-session --case <case> --review <review> --name <name>
python -m feishu_shadow_agent eval promote --config config.yaml --type full-chain --case <case> --review <review> --name <name>
```

promote 按 type 分别校验 review 中的 Evaluation Scenario、labels schema 和跨文件约束，验证完成后将两者拆分并原子创建 golden 目录。Ingress 的 scenario 从 `--run` 读取，`--review` 仅提供 labels。失败不得留下可被 runner 识别的半成品 golden。golden 名称不能依靠路径字符串判定状态。不增加 `review_complete`、`human_reviewed` 等状态字段；成功 promote 本身就是确认边界。

当前工作树中的旧 eval schema 尚未成为发布契约，本地也没有需要保留的 `data/evals` artifact。实现直接以本文定义作为首个正式 v1，不保留 `raw_seed.json`、store snapshot、旧 review 结构或 `--labels` 等兼容分支。

## 11. 测试与验收

默认测试使用 fake Lark client 和 fake backend，保持无网络、无真实发送、无真实模型调用。

必须覆盖：

1. Message Eligibility eval 与 daemon 生产 policy 对同一 normalized message 和 acquisition sources 给出相同 decision/reason precedence，且 policy 不访问 DB。
2. live ingress 的分页、固定时间窗、Lark source 观测和完整 run-directory snapshot replay。
3. offline ingress 固定 `group_at_me` sources，只重算 explicit active-watch scenario 和纯 Eligibility policy，且零网络、零 production DB 访问。
4. judge 失败 fallback 覆盖 timeline 每条消息。
5. ingress promote 的缺失、重复、非法 decision、缺少 mismatch reason。
6. router 的显式 ingestion source、production route 六态（含确定性 owner takeover）、deterministic/model 分支、task fixture 派生状态、alias identity 和 draft/golden 退出码。
7. capture 不生成 production store snapshot，只导出 scenario 显式引用的最小 task/watch-key/message 数据。
8. task session initial 有序 `message_ids` 的完整 prompt 和末条 current-message 语义。
9. resume 的有序 setup -> real session id -> 单条 target-only prompt，且只评分 target。
10. task-session 原始 proposed reply、reply target、initial task-label 结构可用性、精确 answerability/watch-action 匹配、golden reference-answer 交叉校验，以及 semantic judge 使用实际 config backend 和 `session_id = null` 的隔离调用。
11. full-chain 的逐消息 source/顺序、setup-created task alias、setup 错误中止、target baseline 清理、无 dispatcher/无网络发送、raw/effective reply 分离、postprocess/gate 和 would-send trace。
12. captured type-specific review 的 draft 直接运行、artifact 结构判定、run-id 碰撞和 generic promote 的 schema/原子性。
13. 同一 golden 在不同墙上时间重跑时使用相同 Evaluation Clock，并得到相同确定性 lifecycle/routing 输入。
14. `--repeat` 的 trial 间 store/session 隔离、确定性 DB 重建、临时状态清理、Trial Evidence Bundle、pass/fail/error 聚合和 all-pass case 判定。
15. resource fixture 的 raw reference/SHA-256/promotion 校验、trial-local 复制、task-session row 重建和 full-chain 零网络资源处理。
16. Case Baseline Config 与 Evaluation Run Config 的优先级、hash 报告、owner identity 阻断和基线重跑。
17. Evaluation Run Config 的 read-only/full-access tool permissions 传递、report 记录、repeat 警告和 Dispatcher 安全边界。

本地验收：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
git diff --check
```

真实 E2E 只作为显式人工流程：先 capture 或 `run-ingress --dry-run-backend`，检查私密 artifact 和 would-send 结果，再允许真实 backend。默认 pytest 永远不要求飞书凭证。

## 12. 实施顺序

1. Artifact 基础设施：metadata/provenance、run id、config 敏感扫描、最小 scenario export、Temporary Eval Store、文档骨架。
2. Message Eligibility：完整群窗口、timeline 内 acquisition sources、生产共用 policy、judge review labels、promote、golden replay。
3. Capture + Router：候选列表、按 message id 上下文、review template、task alias、router runner。
4. Task session + Full chain：initial/resume、结构评分、语义 judge、临时 baseline、无 dispatch 全链路、批量运行。

## 13. 非目标

- 不修改生产 prompt 的上下文策略。
- 不增加 trace-only context provider。
- 不提供 prompt variant registry 或自动 compare。
- 不提供 suite manifest、artifact upload、UI、DB migration 或 P2P Message Eligibility eval。
- 不把语气作为回答质量评分维度。
- 不对 task label 做语义评分或精确文本匹配；只检查 initial output 的结构可用性。
- 不在默认测试中运行真实飞书、真实模型或真实发送。
