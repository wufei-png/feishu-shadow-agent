# Feishu Shadow Agent 实现计划拆分大纲

日期：2026-06-22

本文是 `feishu-shadow-agent` MVP 实现前的总纲计划索引。当前仓库的设计输入以
[MVP 设计](../specs/feishu-shadow-agent-mvp-design.md) 和
[MVP 流程图](../specs/feishu-shadow-agent-flows.md) 为准。

## 1. 拆分判断

当前 repo 只有文档，MVP spec 已覆盖运行形态、身份边界、ingest、任务归属、Hermes、审批、发送幂等和 CLI。这个实现跨越工程脚手架、SQLite 状态机、飞书 CLI 封装、AI 编排和对外发送闭环，不适合一次计划模式覆盖到底。

建议总共使用 **5 次计划模式**：

1. **P0 总纲**：沉淀计划拆分、实施顺序、计划文件命名，也就是本文。
2. **P1 Foundation**：先把可运行、可测试、可诊断的本地骨架搭起来。
3. **P2 Ingestion And Routing**：再实现 daemon tick 骨架、消息进入、资源状态、任务归属和 active watch。
4. **P3 Hermes And Approval**：接入 AI 处理、回复 gate、SendComposer、审批队列和 owner 私聊命令。
5. **P4 Dispatch And CLI Hardening**：最后闭合真实发送、读回审计、CLI hardening 和端到端验证。

## 2. 计划文件

后续每期进入实现前，先产出对应细化计划，再按该计划实现：

| 阶段 | 计划文件 | 目标 |
| --- | --- | --- |
| P1 | `docs/plans/p1-foundation.md` | Python 包、配置、SQLite、日志、Store、`FeishuClient`/`LarkCliClient`、`doctor`、CLI/daemon 骨架 |
| P2 | `docs/plans/p2-ingestion-routing.md` | daemon tick skeleton、approval inbox checkpoint 占位、group `@me`、P2P、active watch、normalize、resource metadata/status、checkpoint、CandidateCollector、watch_keys、owner takeover |
| P3 | `docs/plans/p3-hermes-approval.md` | TaskRouter、Task Session、Hermes JSON schema、reply/per-chat/resource gate、SendComposer、ApprovalRequest、owner bot 通知、approval inbox 命令状态流转 |
| P4 | `docs/plans/p4-dispatch-cli-hardening.md` | send dispatcher、dry-run/actual send/readback、idempotency、`status/replay/approve/reject/send/config show`、端到端验证 |

## 3. 阶段边界

### P1 Foundation

先读：

- `docs/specs/feishu-shadow-agent-mvp-design.md`：第 1、2、3、9、10、21、22、23、24 节。
- `docs/specs/feishu-shadow-agent-flows.md`：第 1、2 节。

交付：

- 创建 Python package 和 `python -m feishu_shadow_agent` 入口。
- 提供 `config.example.yaml`、真实 `config.yaml` gitignore 策略、`ConfigService`，配置 schema 明确覆盖 `chats`、`reply_policy`、`tool_permissions`。
- 建立 SQLite schema/migration、基础 `Store`、JSONL logger、run/health 记录。
- 封装 `FeishuClient` 接口和 MVP `LarkCliClient` command builder。
- 提供 CLI skeleton 和 health-only/no-op `daemon` 子命令骨架，不拉消息、不发送消息。
- 实现 `doctor`，包含 `lark-cli` 路径、版本、auth、scope、SQLite、Hermes reachability、配置 schema 检查。

验收：

- 单元测试覆盖配置 schema、migration、唯一约束、日志格式和 command builder。
- `python -m feishu_shadow_agent doctor` 可运行，默认不发送真实消息。

### P2 Ingestion And Routing

先读：

- `docs/specs/feishu-shadow-agent-mvp-design.md`：第 4、5、6、14、15、16、17、18、24 节。
- `docs/specs/feishu-shadow-agent-flows.md`：第 2、3、4 节。

交付：

- 实现真实 `daemon` tick skeleton，按 approval inbox、group ingest、p2p ingest、active watch、dispatch 顺序编排；dispatch 阶段只保留 no-op/占位。
- 实现 approval inbox placeholder 日志；P2 不推进真实 `approval_inbox` checkpoint，
  等 P3 真正拉取/入库 owner 命令后再写 checkpoint。
- 实现 group `@me`、P2P 和 active task watch 的拉取入口。
- 实现 message normalize、sender role、`direct_mention`/`at_all`、self-loop guard。
- 实现资源 metadata 提取、bot resource download 尝试和 `resources.download_status` 记录；是否阻断回复、是否通知 owner 放到 P3 gate 判断。
- 实现 checkpoint drain 成功后推进、失败不推进。
- 实现 `CandidateCollector`、deterministic shortcut、`watch_keys`、closed recall、owner takeover。

验收：

- 用 fake Feishu 返回覆盖分页、乱序、重复 message_id、checkpoint 失败回滚。
- 覆盖 owner takeover 取消 pending send/approval、bot/agent 自消息不触发新任务、bot 不在群时只记录资源状态、不在 P2 自动回复或通知。

### P3 Hermes And Approval

先读：

- `docs/specs/feishu-shadow-agent-mvp-design.md`：第 7、8、9、10、11、12、13、15、19、20 节。
- `docs/specs/feishu-shadow-agent-flows.md`：第 3、5、6 节。
- `docs/plans/p3-hermes-approval.md`：P3 v2 的实现边界和验收条件。

交付：

- 实现 `AgentBackend` 抽象，首个 provider 为 Hermes CLI backend，区分无状态 TaskRouter 和一任务一会话 Task Session。
- 迁移 `agent_session_id`：新任务置空，旧 `feishu-task-*` 视为未初始化，首次 Task Session 成功后保存 agent backend 真实 session id。
- 定义并校验 Hermes 严格 JSON 输出 schema。
- 新增 `TaskProcessingService`，把 P2 placeholder route 接到 Hermes TaskRouter，把确定性 route 接到 Task Session。
- 实现 `reply_policy` gate、per-chat policy gate、Python 资源 preflight 和审批降级。
- 实现 `SendComposer`，由 Python 统一清理 Hermes 输出里的 `<at ...>` / `@所有人`，并生成 `<at user_id="...">显示名</at>` 前缀。
- 实现 `ApprovalService`、ApprovalRequest 状态机、owner bot 通知 payload。
- 实现 approval inbox 命令解析和事务化状态流转：`/approve <id>`、`/reject <id>`、`/send <task_id> <最终回复>`。

验收：

- 覆盖 TaskRouter 的 `new_task|attach_task|reopen_task|close_task|ignore|ambiguous`。
- 覆盖 Hermes 输出 schema 失败、非法 `reply_target_message_id`、task_id shortcut 多 pending approval 冲突。
- 覆盖 per-chat 关闭自动回复、资源缺失需要 owner 通知、Hermes 误生成 @ 时清理或降级审批。

### P4 Dispatch And CLI Hardening

先读：

- `docs/specs/feishu-shadow-agent-mvp-design.md`：第 6、7、8、24、25 节。
- `docs/specs/feishu-shadow-agent-flows.md`：第 2、5、6、7 节。

交付：

- 实现 `Dispatcher`，包括 send action 互斥、dry-run、actual send、readback 验证和 warning 记录。
- 补齐 `daemon --dry-run`、`--dry-run --send-owner-notifications` 的发送侧语义。
- 实现 `status`、`replay`、`approve`、`reject`、`send`、`config show --redacted`。
- 端到端 fake Feishu/Hermes dry-run 验证 MVP 主流程。

验收：

- 同一 `task_id + reply_target_message_id` 只有一个 pending/sending send action。
- 所有对外回复都先 dry-run，发送后读回验证 `reply_to` 和 mentions。
- 端到端 fake Feishu/Hermes 覆盖 approval inbox、group ingest、p2p ingest、active watch、dispatch 的完整顺序。

## 4. 全局约束

- MVP 继续遵守 specs：不上 SDK、不做 LaunchAgent、不做 Web UI、不做 `config set`、不扫全群、不处理 `@All`。
- `lark-cli` command surface 以当前机器和 `doctor` live check 为准；已知当前验证版本是 `1.0.56`，`+chat-messages-list` 使用 `--order asc|desc`。
- 对外回复统一走 `im +messages-reply`，owner 通知和审批私聊使用 bot 身份。
- 资源下载保持两阶段：user 读消息，bot 下载 `messages-resources-download`。
- Hermes 不生成 @；Python `SendComposer` 负责 mention。
- 所有真实发送必须有 dry-run、idempotency key 和读回审计。
