# 模型评测工作流

评测数据位于已忽略的 `data/evals/`，包含真实飞书消息和配置副本，不得提交。
完整设计和决策见
[`docs/plans/p20-production-parity-model-evals.md`](plans/p20-production-parity-model-evals.md)。

## 1. 核心边界

- `capture` 和 live `run-ingress` 可以访问飞书；`--snapshot`、golden replay 和全部 model runner 不访问飞书。
- Router、Task Session、Full Chain 每个 trial 都从空的文件型 SQLite 重建状态，不复制、不修改生产 DB。
- case 内 `config.yaml` 是不可变的 Case Baseline Config；命令行 `--config` 是唯一 Evaluation Run Config，两者不合并。
- Task Session 和 Full Chain 的 model candidate 与 semantic judge 使用同一 run config/backend；judge 始终以 `session_id: null` 运行。
- Full Chain 不运行 Dispatcher，不发送飞书消息或 owner notification，但 `tool_permissions: full_access` 的 Agent 工具仍可能产生外部副作用。

Artifact 状态由文件结构决定：

- draft：存在当前 type 的 `*.review.yaml`。
- golden：存在 `eval_case.yaml`、`labels.yaml`、`provenance.yaml`。
- none：只有 `eval_case.yaml`。

旧的 `raw_seed.json`、`raw_context.jsonl`、`store_snapshot.sqlite3`、`--labels` 和组合式旧 labels schema 不受支持。

## 2. Capture

列出最近 `@owner` 的群消息和 P2P 消息：

```bash
python -m feishu_shadow_agent eval capture \
  --config config.yaml \
  --lookback-days 2 \
  --limit 20
```

人工选择 message id 后落盘：

```bash
python -m feishu_shadow_agent eval capture \
  --config config.yaml \
  --message-id om_xxx \
  --context-before 20 \
  --context-after 0
```

输出：

```text
data/evals/captured/<case_id>/
  messages.jsonl
  resources/
  router.review.yaml
  task_session.review.yaml
  full_chain.review.yaml
  REVIEW.md
  config.yaml
  metadata.yaml
```

`messages.jsonl` 每行就是一个去重后的原始飞书消息，不再包 `{role, raw}`。Capture 可只读查询生产 DB 来建议 Router task fixture，但不会复制生产数据库。三个 review 的 scenario 相互独立，可直接作为 draft 运行；未填写 labels 时照常运行链路，`passed` 为 `null`。

## 3. Ingress

Ingress 只评估群消息 Acquisition + Eligibility，不运行 Router。

Live 相对窗口：

```bash
python -m feishu_shadow_agent eval run-ingress \
  --config config.yaml \
  --chat-id oc_xxx \
  --lookback-days 2
```

Live 固定窗口：

```bash
python -m feishu_shadow_agent eval run-ingress \
  --config config.yaml \
  --chat-id oc_xxx \
  --start 2026-07-10T09:00:00+08:00 \
  --end 2026-07-10T11:00:00+08:00
```

固定 snapshot 重放：

```bash
python -m feishu_shadow_agent eval run-ingress \
  --config config.yaml \
  --snapshot data/evals/ingress-runs/<run_id>
```

`--snapshot` 必须指向完整 ingress run 目录，不能指向单个 raw 文件。Snapshot 冻结 Lark 返回的 `group_at_me` source，并用执行 live run 时捕获的 active task/watch-key fixture 重新计算本地 `active_watch` source；该模式不访问飞书或生产 DB。它不是历史 daemon acquisition 日志：早于 capture 时已经关闭或过期的 task 无法由当前 store 还原，因此历史窗口中的 `active_watch` 是 capture-time baseline 下的反事实重算，不代表消息当时实际被 daemon 拉取。

Run 输出：

```text
data/evals/ingress-runs/<run_id>/
  raw_messages.jsonl
  eval_case.yaml
  ingress_timeline.yaml
  labels.review.yaml
  run_report.yaml
  REVIEW.md
  config.yaml
  metadata.yaml
```

Timeline 每条消息包含：

```yaml
sources: [group_at_me, active_watch]
current_decision: kept | dropped
reason_code: not_acquired | self_message | at_all_suppressed | owner_intervention | direct_owner_mention | active_watch_message | non_direct_mention
```

Judge 读取同群同窗完整 timeline，为每条消息生成一个 review label。Judge 失败时仍生成 `expected_decision = current_decision` 的完整 fallback 文件，并把错误写入 `run_report.yaml`。

人工只修改 `expected_decision` 和 `review_reason`。一致项的 reason 必须为空；不一致项必须非空。Promote：

```bash
python -m feishu_shadow_agent eval promote \
  --config config.yaml \
  --type ingress \
  --run data/evals/ingress-runs/<run_id> \
  --review data/evals/ingress-runs/<run_id>/labels.review.yaml \
  --name group-filter-case
```

Golden replay 不调用 judge：

```bash
python -m feishu_shadow_agent eval run-ingress \
  --config config.yaml \
  --golden data/evals/golden/ingress/group-filter-case
```

报告包含 TP/TN/FP/FN、precision、recall 和逐条 mismatch。

## 4. Router

Router scenario 只重建 target 之前的 task state：

```yaml
schema_version: eval_case_v1
case_type: router
target:
  message_id: om_target
  source: group_at_me | active_watch | p2p
tasks:
  task_1:
    status: watching | closed | closed_by_owner | human_taken_over
    task_label: "任务标题"
    message_ids: [om_1, om_2]
```

Golden labels：

```yaml
schema_version: router_labels_v1
route: new_task | attach_task | reopen_task | ignore | ambiguous | human_taken_over
task_key: task_1
```

`task_key` 只在 attach/reopen/human-taken-over 时存在。Runner 使用生产 `MessageRouter`；只有生产路径返回 Router placeholder 时才调用模型 router，不调用 judge。

## 5. Task Session

Initial：

```yaml
schema_version: eval_case_v1
case_type: task-session
mode: initial
message_ids: [om_1, om_2]
resources: []
```

Resume：

```yaml
schema_version: eval_case_v1
case_type: task-session
mode: resume
setup_message_ids: [om_1, om_2]
target_message_id: om_3
resources: []
```

Resume 会把全部 setup messages 作为一个真实 initial turn 执行。只有拿到真实 provider session id 后才 attach target，并按生产规则只把 target 送入 resume prompt；setup 失败不会回退为 initial。

Golden labels：

```yaml
schema_version: task_session_labels_v1
answerability: auto_reply | needs_owner | no_reply
watch_action: keep_watching | close
reference_answer: "标准答案"
```

`auto_reply` / `needs_owner` 必须有非空 `reference_answer`；`no_reply` 必须省略。程序先检查 answerability、watch action、reply target、空回复、initial task label 和禁止 mention。结构合法后，semantic judge 只判断事实差异：omission、unsupported addition、contradiction、overcommitment。只有 `verdict: pass` 通过。

## 6. Full Chain

```yaml
schema_version: eval_case_v1
case_type: full-chain
setup:
  - message_id: om_1
    source: group_at_me
target:
  message_id: om_2
  source: active_watch
resources: []
```

每条 setup message 都从 Eligibility 已完成的生产入口运行 `process_eligible_raw_message -> Router -> Task Session`，按任务创建顺序分配 `task_1`、`task_2` 等 alias；setup 只建立状态，不评分。Target 先精确评分 Router route/alias，Router 不匹配时跳过 Task Session semantic judge。

Full Chain 的 semantic candidate 是 reply postprocess、composer、policy gate 和 approval/action 处理后的 effective reply，不是原始 `proposed_reply`。报告同时保留 raw reply、effective reply 和 would-send trace。资源由仅能读取 case fixture 的离线 Feishu client 提供；任何其他飞书调用直接失败。

## 7. 运行与 Repeat

单 case：

```bash
python -m feishu_shadow_agent eval run-router --config config.yaml --case <case> --repeat 3
python -m feishu_shadow_agent eval run-task-session --config config.yaml --case <case> --repeat 3
python -m feishu_shadow_agent eval run-full-chain --config config.yaml --case <case> --repeat 3
```

批量：

```bash
python -m feishu_shadow_agent eval run-task-session \
  --config config.yaml \
  --cases data/evals/golden/task-session \
  --repeat 3 \
  --label prompt-v2
```

每个 trial 使用新 backend 对象、新 provider session 和新 Temporary Eval Store。固定 scenario 的 DB 插入顺序、row id 和业务时间由消息 `sent_at` 确定。每个 case 通过互斥锁使用稳定的 `.trial-slots/<case-hash>/current` 访问别名，使 SQLite URI、resource path 和 prompt hash 不受随机临时目录影响；别名只指向当前 trial，同一 case 的并发运行会串行化。上一个 trial 的 DB、资源和 session 不会进入下一个 trial。

Run 保留：

```text
report.yaml
trials/<n>/report.yaml
trials/<n>/events.jsonl
trials/<n>/prompts/   # 仅 debug.save_full_agent_io: true
```

证据写入后，无论成功或失败都会删除访问别名、临时 DB 和 trial-local resource；`.trial-slots` 中只保留不含业务数据的锁文件。报告聚合 `passed_trials`、`failed_trials`、`error_trials`、`pass_rate` 和 semantic difference counts；golden 只有全部 trials 通过才通过。

该边界保证 eval-owned DB 隔离和可重建，不保证模型输出逐字一致，也不保证 `full_access` Agent 工具外部副作用幂等。`full_access + --repeat > 1` 会输出非阻断警告。

退出码：

- `0`：draft/none 正常完成，或 golden 全部通过。
- `1`：golden 存在结构或语义失败。
- `2`：artifact、backend、judge、schema 或状态运行错误。

`--dry-run-backend` 只验证输入构造和链路，不代表模型能力。

## 8. Promotion

Model case：

```bash
python -m feishu_shadow_agent eval promote \
  --config config.yaml \
  --type task-session \
  --case data/evals/captured/<case_id> \
  --review data/evals/captured/<case_id>/task_session.review.yaml \
  --name resume-followup
```

Promotion 是唯一人工确认边界。它严格校验 scenario、golden labels、message 时间顺序、owner identity 和 resource SHA-256，只复制 scenario 引用的最小 messages/resources，并通过 staging 目录原子创建 golden。失败不会留下可运行的半成品，也不会覆盖现有同名 golden。Promotion 在再次复制 source `config.yaml` 前也会重跑敏感字段检查；确实需要保留敏感值时必须显式增加 `--allow-sensitive-config`。

## 9. 敏感配置

Capture、ingress 和 model run 都会复制实际 `config.yaml`。检测到 token、secret、password 等非空字段时默认拒绝复制；确认输出目录的隐私边界后，显式增加：

```bash
--allow-sensitive-config
```

Model run 目录也复制实际 Evaluation Run Config，用于记录 `run_config_hash`；report 同时记录 `case_config_hash` 和 `config_changed`。Metadata 保存原 `config_base_dir`，因此未修改的 config 副本仍按原目录解析相对 working-dir/profile/skill 路径。要重跑 case 基线，显式使用 `<case>/config.yaml`。
