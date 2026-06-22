# P2 Ingestion And Routing

## Summary

- Upgrade the P1 daemon from health-only/no-op ticks to the real P2 tick order:
  approval inbox placeholder, group `@me` ingest, P2P ingest, active watch, dispatch placeholder.
- P2 owns ingestion, normalization, resource status, checkpointing, task ownership, `watch_keys`,
  deterministic routing shortcuts, and owner takeover.
- P2 does not call Hermes, parse approval commands, compose replies, notify owner, or send external messages.
- The approval inbox placeholder only logs stage execution; it does not write the real
  `approval_inbox` checkpoint until P3 implements owner command fetch and ingest.

## Implementation Notes

- `FeishuClient` exposes business methods for message search, chat/thread listing, and bot resource download.
  `LarkCliClient` keeps command construction separate and maps JSON output to `MessagePage`.
- SQLite migration `0002_ingestion_routing` adds message routing fields, task watch fields, and `routing_audits`.
  Store APIs own all P2 writes for messages, resources, tasks, task messages, watch keys, approvals, actions,
  checkpoints, and route audit rows.
- `IngestionService` drains every page before advancing a checkpoint. Messages are processed in
  `create_time asc, message_id asc` order, and duplicate `message_id` rows are audited but not routed again.
- `MessageNormalizer` marks sender role, direct mention, `@all`, thread/reply target, mentions, text, and
  image/file resource metadata.
- `MessageRouter` uses SQLite-only candidate collection. P2P single active task, thread key, and `reply_to`
  message key are deterministic shortcuts. Other ambiguous cases record `router_placeholder` for P3.
- Owner messages are checked before normal routing. A uniquely related active task is marked
  `human_taken_over`, pending send actions are cancelled, and pending send approvals are expired.

## Validation

- Existing P1 tests remain green.
- P2 tests cover pagination drain, checkpoint rollback on failure, normalization, self/owner routing,
  deterministic shortcuts, duplicate suppression, resource download status, owner takeover, and daemon stage order.

## Acceptance

```bash
.venv/bin/python -m pytest
```

## Full Plan Details

# P2 Ingestion And Routing 计划

## Summary

- 在 P1 可运行骨架上，把 daemon 从 no-op tick 升级为真实 tick 编排：`approval inbox -> group @me -> p2p -> active watch -> dispatch placeholder`。
- P2 只负责“拉取、归一化、入库、资源状态、任务归属、watch_keys、owner takeover”；不接 Hermes Task Session、不做审批命令状态流转、不真实发送。
- 当前基线确认：`.venv/bin/python -m pytest` 通过 23 项；本机 `lark-cli` 为 `1.0.56`，`+chat-messages-list` 使用 `--order`，和现有 builder 一致。

## Key Changes

- 扩展 `FeishuClient` 协议和 `LarkCliClient`：
  - 暴露 `search_messages`、`list_chat_messages`、`list_thread_messages`、`download_resource` 等业务方法，内部复用现有 argv builder。
  - 所有 ingest 调用保持 `--as user`；资源下载固定 `--as bot`；P2 不新增 SDK 路径。
  - 增加 fake client/test fixtures，测试 daemon 不直接绑定真实 CLI。

- 增加 P2 业务模型与 Store API：
  - 定义 `IncomingMessage`、`NormalizedMessage`、`ResourceRef`、`RouteDecision`、`TaskCandidate`、`TaskRecord` 等轻量 dataclass。
  - 为 `messages`、`resources`、`tasks`、`task_messages`、`task_watch_keys`、`actions`、`approvals` 增加事务级 upsert/query 方法。
  - 若现有 schema 字段不足，新增一条 migration 补齐 P2 必需字段，例如 task `watch_until`、thread id/parent id、routing audit 可查询字段；保留 0001 不重写。

- 实现 ingest + normalize：
  - `group_at_me` 使用 `messages-search --chat-type group --is-at-me --query ""`。
  - `p2p` 使用 `messages-search --chat-type p2p --query ""`。
  - 每个入口按 checkpoint 窗口拉取：`start = last_success_at - overlap_seconds`，`end = now`，drain 全部分页后才推进 checkpoint。
  - 同一批消息按 `create_time asc, message_id asc` 逐条处理，依赖 `message_id` 去重。
  - normalizer 标记 `sender_role`、`direct_mention`、`at_all`、`thread_id`、`reply_to_message_id`、mentions、文本摘要、附件 metadata。
  - `at_all` 默认 suppressed，只入库和审计，不创建任务、不通知 owner。

- 实现资源 metadata/status：
  - 只处理进入任务流程的 root message 和必要 follow-up 资源，不扫完整上下文窗口。
  - 提取 image/file 的 `file_key`、类型和 raw metadata，upsert 到 `resources`。
  - chat policy `bot_joined=true` 时尝试 bot 下载到安全相对路径；成功记录 `downloaded/path/sha256`。
  - `bot_joined=false`、234002/234040 或其他失败只记录 `download_status` 与错误；是否阻断回复、是否通知 owner 留到 P3 gate。

- 实现 routing / watch：
  - `CandidateCollector` 只读 SQLite，不调用 Hermes。
  - 使用单一 `watch_keys`：`user:<open_id>`、`msg:<message_id>`、`thread:<thread_id>`。
  - 确定性 shortcut：
    - P2P 同一 chat 只有一个 active task 且在 `watch_until` 内，直接 attach。
    - group thread id 唯一命中 active task，直接 attach。
    - `reply_to` 的 `msg:<id>` 唯一命中 active task，直接 attach。
  - 其他复杂情况在 P2 写 routing audit 并落到 `ambiguous/router_placeholder`，不调用 Hermes；P3 再接 TaskRouter。
  - 新触发事件没有候选时创建 task，初始化 `watch_keys` 和 `task_messages`。
  - closed recall 只做候选检索和 audit 占位，不做 LLM 判定。

- 实现 owner takeover：
  - owner 消息先于 CandidateCollector 判断。
  - 当 `reply_to`、thread、或 P2P 唯一 active task 能确定关联任务时，记录 `human_taken_over`，关闭/标记 task，取消未发送的 `send_reply` action，过期 pending `send_reply` approval。
  - 不能确定关联任务的 owner 群消息只入库并记录 `owner_message_not_task_intervention`。

- 改造 daemon tick：
  - `Daemon.run_one_noop_tick` 替换为 `run_one_tick`，按 P2 顺序执行。
  - approval inbox 阶段只拉取 owner 与 bot P2P 消息并推进 `approval_inbox` checkpoint；不解析 `/approve|/reject|/send`。
  - active watch 从 `tasks` 表发现监听目标，按 chat/thread 合并拉取一次，再逐条进入同一 routing pipeline。
  - dispatch 阶段保留 no-op 日志和 pending action 计数，不发送。

## Public Interfaces

- CLI：
  - `python -m feishu_shadow_agent daemon --dry-run --config ...` 在 P2 会真实拉消息、写库、下载可下载资源，但不发送对外回复、不发 owner 通知。
  - `--dry-run --send-owner-notifications` 在 P2 仍不发送通知，因为通知和 approval 状态流转属于 P3。
  - `status/replay/approve/reject/send` 继续保持 P4 预留。

- Config：
  - 不新增广泛配置面。
  - 复用现有 `daemon.overlap_seconds`、`chats.<chat_id>.bot_joined/resource_download`、`retention.resource_days`。
  - 如需要 page size，先用代码常量 `50`，不引入 YAML 字段。

- SQLite：
  - checkpoint key 固定为：
    - `approval_inbox`
    - `ingest.group_at_me`
    - `ingest.p2p`
    - `active_watch.chat.<chat_id>`
    - `active_watch.thread.<thread_id>`
  - task 状态至少覆盖：`watching`、`waiting_approval`、`closed`、`closed_by_owner`、`human_taken_over`。
  - routing audit 可通过 `actions` 或新增专用轻表记录，必须能支持后续 `status/replay` 查看 `route_reason`、`candidates_count`、`shortcut_hit`、`router_called=false`、`matched_by`、`target_task_id`。

## Test Plan

- 保留并通过现有 P1 测试：`.venv/bin/python -m pytest`。
- 新增 fake Feishu 分页测试：
  - group @me / p2p 多页 drain 完才推进 checkpoint。
  - 分页、入库、routing 任一失败时 checkpoint 不推进。
  - 乱序、重复 `message_id` 不重复建任务或关联消息。
- 新增 normalize 测试：
  - direct mention、`@All`、owner/bot/agent/external sender role。
  - thread id、reply_to、mentions、附件 metadata 提取。
  - bot/agent 自消息只审计，不进入 routing。
- 新增 routing 测试：
  - P2P 单 active task shortcut。
  - group thread shortcut。
  - `reply_to` msg key shortcut。
  - 多候选或换题可能性写 `ambiguous/router_placeholder`，不调用 Hermes。
  - 新触发创建 task、初始化 `watch_keys`，closed recall 只出候选/audit。
- 新增 owner takeover 测试：
  - owner 在原 chat/thread 接管时取消 pending send action、过期 pending send_reply approval、关闭 task。
  - owner 群消息无法唯一关联时只入库审计。
- 新增资源测试：
  - bot joined 下载成功记录 path/hash/status。
  - bot 不在群、234002/234040、下载失败只记录资源状态，不自动回复、不通知 owner。
- 新增 daemon 顺序测试：
  - 单 tick 严格按 approval inbox、group ingest、p2p ingest、active watch、dispatch placeholder 执行。
  - active watch 同 chat/thread 多 task 合并拉取一次。

## Assumptions

- P2 可以创建 `docs/plans/p2-ingestion-routing.md` 作为计划落地文件，但实现不改 P1 计划和总纲。
- P2 不引入 Hermes API 调用；所有需要语义判断的分支先用 `router_placeholder` 审计，P3 接入。
- P2 不做真实发送、owner 通知、审批命令解析、SendComposer、reply policy gate。
- 本机 `lark-cli --help` 是命令参数的当前事实；实现时以现有 `LarkCliClient` builder 为入口，并用测试固定 `--order`、`--no-reactions` 等行为。
