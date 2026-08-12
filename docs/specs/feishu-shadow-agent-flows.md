# Feishu Shadow Agent MVP 流程图

本文是 [MVP 设计](./feishu-shadow-agent-mvp-design.md) 的 Mermaid 流程图补充。主 spec 描述约束和规则；本文只沉淀数据如何在系统里流动，以及关键子流程如何闭环。

<a id="overall-flow"></a>

## 1. 整体数据流程

```mermaid
flowchart LR
  subgraph Feishu["Feishu"]
    GroupMsg["群聊 @ owner 消息"]
    P2PMsg["P2P 私聊消息"]
    OwnerDM["owner 与 bot 私聊"]
    ReplyTarget["原消息 reply target"]
  end

  subgraph LarkCli["lark-cli"]
    UserRead["--as user 读取/搜索/回复"]
    BotOps["--as bot 通知/审批/群回复/资源下载"]
  end

  subgraph Agent["Python daemon"]
    Health["health check"]
    Ingest["ingest + normalize"]
    Store["SQLite Store"]
    HumanTakeover["loop guard / human_taken_over"]
    CancelPending["cancel pending send/approval + close task"]
    Match["CandidateCollector / TaskMatcher"]
    Router["Hermes TaskRouter"]
    TaskSession["Hermes Task Session"]
    Policy["reply policy + gates"]
    Composer["SendComposer"]
    Approval["ApprovalRequest queue"]
    Dispatcher["pending actions dispatch"]
    Logs["JSONL logs + audit"]
  end

  GroupMsg --> UserRead
  P2PMsg --> UserRead
  OwnerDM -->|owner 命令| BotOps
  UserRead --> Ingest
  BotOps --> Ingest
  Health --> Store
  Ingest --> Store
  Ingest --> HumanTakeover
  Store --> HumanTakeover
  HumanTakeover -->|未接管| Match
  HumanTakeover -->|接管| CancelPending
  CancelPending --> Store
  Store --> Match
  Match -->|确定归属| TaskSession
  Match -->|归属不确定| Router
  Router --> TaskSession
  TaskSession --> Policy
  Policy -->|低风险高置信| Composer
  Policy -->|不确定或高风险| Approval
  Approval --> Dispatcher
  Composer --> Dispatcher
  Dispatcher -->|对外回复| UserRead
  Dispatcher -->|bot 群回复/owner 通知| BotOps
  UserRead --> ReplyTarget
  BotOps -->|bot 群回复| ReplyTarget
  BotOps -->|owner 通知/审批私聊| OwnerDM
  Store --> Logs
  HumanTakeover --> Logs
  CancelPending --> Logs
  TaskSession --> Logs
  Dispatcher --> Logs
```

<a id="daemon-tick-flow"></a>

## 2. Daemon tick 流程

```mermaid
flowchart TD
  Start["daemon start"] --> StartupHealth["并发 startup health check"]
  StartupHealth --> HealthOK{"critical check 通过"}
  HealthOK -->|否| FailClosed["fail closed 不启动"]
  HealthOK -->|是| Loop["进入 tick loop"]

  Loop --> RuntimeHealth{"运行中 critical health ok"}
  RuntimeHealth -->|否| Pause["暂停 ingestion / sending"]
  Pause --> Recheck["按 retry_interval 重检"]
  Recheck --> RuntimeHealth

  RuntimeHealth -->|是| ApprovalInbox["1. approval inbox<br/>成功后推进 approval_inbox checkpoint"]
  ApprovalInbox --> GroupIngest["2. group_at_me ingest<br/>分页 drain + 时间升序处理<br/>成功后推进 ingest.group_at_me checkpoint"]
  GroupIngest --> P2PIngest["3. p2p ingest<br/>分页 drain + 时间升序处理<br/>成功后推进 ingest.p2p checkpoint"]
  P2PIngest --> ActiveWatch["4. active task watch<br/>按 chat/thread 合并拉取<br/>成功后推进 active_watch.* checkpoint"]
  ActiveWatch --> Dispatch["5. pending actions dispatch<br/>send 互斥"]
  Dispatch --> Sleep["sleep tick_interval"]
  Sleep --> Loop
```

<a id="message-task-routing-flow"></a>

## 3. 消息进入与任务归属

```mermaid
flowchart TD
  Incoming["IncomingMessage batch<br/>create_time asc, message_id asc"] --> Normalize["normalize message<br/>标记 sender_role"]
  Normalize --> SenderRole{"sender_role"}
  SenderRole -->|bot/agent| SelfAudit["messages upsert<br/>关联 action/task<br/>route=ignore self_message"]
  SenderRole -->|owner| OwnerCheck{"owner 在原 chat/thread 直接回复?"}
  OwnerCheck -->|是| OwnerRelated{"确定关联 active task?"}
  OwnerRelated -->|是| HumanTakeover["messages upsert<br/>human_taken_over<br/>取消 pending send/approval<br/>关闭 task"]
  OwnerRelated -->|否| OwnerAudit["messages upsert<br/>route=ignore owner_message_not_task_intervention"]
  OwnerCheck -->|否| OwnerAudit
  SenderRole -->|external| Suppressed{"at_all 或非处理入口"}
  Suppressed -->|是| RecordSuppressed["入库记录 suppressed / ignored"]
  Suppressed -->|否| SaveMsg["messages upsert"]
  SaveMsg --> CandidateCollector["CandidateCollector 纯 SQLite 检索"]

  CandidateCollector --> MatchAudit["写 candidates_count / shortcut_hit"]
  MatchAudit --> Deterministic{"确定性 shortcut 命中"}
  Deterministic -->|thread_id 唯一命中| Attach
  Deterministic -->|reply_to msg 唯一命中| Attach

  Deterministic -->|否| Burst{"burst window 唯一命中<br/>同 chat + 同 sender + 窗口内"}
  Burst -->|是| Attach
  Burst -->|否| CandidateCount{"候选是否明确"}
  CandidateCount -->|无 active 或新触发| Historical["closed task recall 检索最近 7 天"]
  CandidateCount -->|多个或语义可能换题| TaskRouter["Hermes TaskRouter"]
  Historical --> TaskRouter

  TaskRouter --> RouterAudit["写 route_reason / router_called / target_task_id"]
  RouterAudit --> Route{"route"}
  Route -->|new_task| NewTask["创建 task + watch_keys"]
  Route -->|attach_task| Attach
  Route -->|reopen_task| Reopen["reopen task"]
  Route -->|ignore| Ignore["ignore 并记录 reason"]
  Route -->|ambiguous| Approval["降级 owner 审批/确认"]

  Attach --> Include["task_messages 去重关联；结束/取消类消息也先 attach"]
  Reopen --> Include
  NewTask --> Include
  Include --> TaskSession["进入 Hermes Task Session；watch_action=close 时关闭任务"]
```

<a id="resource-download-flow"></a>

## 4. 资源下载与 bot gate

```mermaid
flowchart TD
  Msg["已入库消息"] --> HasResource{"包含图片/文件资源"}
  HasResource -->|否| Continue["继续任务处理"]
  HasResource -->|是| Extract["user 身份读消息并提取 file_key"]
  Extract --> BotKnown{"chat policy 标记 bot_joined"}
  BotKnown -->|否，群聊| NotifyJoin["创建 resource_needs_bot owner notification"]
  BotKnown -->|否，P2P 纯资源消息| WaitContext["保持 watching，等待同任务文字上下文"]
  BotKnown -->|否，P2P 已有明确诉求| TaskWithoutResource["Task Session 使用文字、资源状态与外部证据"]
  TaskWithoutResource --> P2PAnswerable{"Agent 能安全完成"}
  P2PAnswerable -->|是| Continue
  P2PAnswerable -->|needs_owner| OwnerEscalation["关闭自动处理并创建 Owner Escalation"]

  BotKnown -->|是| Download["bot 身份 messages-resources-download"]
  Download --> DownloadOK{"下载成功"}
  DownloadOK -->|是| SaveResource["保存 data/resources + resources 元数据"]
  SaveResource --> Continue
  DownloadOK -->|234040/不可见| NotifyJoin
  DownloadOK -->|其他错误| Retry{"同轮重试未超过上限"}
  Retry -->|是| Download
  Retry -->|否| ResourceFailed["记录 resource_download terminal failed + owner notification"]
```

<a id="hermes-reply-flow"></a>

## 5. Hermes 处理与回复决策

```mermaid
flowchart TD
  TaskInput["task message + resources"] --> ResourceReady{"相关资源全部 downloaded"}
  ResourceReady -->|否| ResourceBlocked["资源阶段阻断：owner notification，不调用 Hermes"]
  ResourceReady -->|是| BuildPrompt["构建 minimal metadata + conversation block"]
  BuildPrompt --> Hermes["Hermes Task Session"]
  Hermes --> Parse{"严格 JSON schema 校验"}
  Parse -->|失败| Approval["创建 send_reply ApprovalRequest"]
  Parse -->|通过| ValidateTarget{"reply_target_message_id 在候选内"}
  ValidateTarget -->|否| Approval
  ValidateTarget -->|是| UpdateTask["初次写 task_label；按 watch_action 更新 watching/close"]

  UpdateTask --> Answerability{"answerability"}
  Answerability -->|auto_reply| Gates{"policy / direct mention / composer gates 通过"}
  Answerability -->|needs_owner| Approval
  Answerability -->|no_reply| WatchOnly["仅更新 watch_until / close"]

  Gates -->|否| Approval
  Gates -->|是| Compose["SendComposer 清理 @ 并生成回复文本"]
  Compose --> PendingAction["创建 pending send action"]
```

<a id="approval-flow"></a>

## 6. 审批与手动发送

```mermaid
flowchart TD
  ApprovalReq["ApprovalRequest pending"] --> NotifyOwner["bot 私聊 owner 通知"]
  NotifyOwner --> Inbox["approval inbox 拉取 owner 命令"]
  Inbox --> Command{"命令类型"}

  Command -->|/approve a_xxx| ResolveApproval["解析 approval_id"]
  Command -->|/approve t_xxx| ResolveTask["task_id shortcut"]
  ResolveTask --> OnePending{"该 task 只有一个 pending approval"}
  OnePending -->|否| AskSpecific["提示使用具体 approval_id"]
  OnePending -->|是| ResolveApproval

  Command -->|/reject id| Reject["标记 rejected 并按规则关闭/继续 watch"]
  Command -->|/send task_id text| ManualSend["创建并批准 send_reply approval"]

  ResolveApproval --> Approve["标记 approved"]
  Approve --> SendAction["创建 pending send action"]
  ManualSend --> SendAction
  Reject --> Audit["写 actions / logs / audit"]
  AskSpecific --> Audit
```

<a id="idempotent-send-flow"></a>

## 7. 幂等发送与读回验证

```mermaid
flowchart TD
  PendingSend["pending send action"] --> InFlight{"同 task_id + reply_target<br/>已有 pending/sending/failed_needs_review?"}
  InFlight -->|是| Noop["no-op / 提示已有 in-flight send"]
  InFlight -->|否| BuildText["SendComposer 生成 text / mention"]
  BuildText --> Idempotency["生成 reply-<short_hash> idempotency key"]
  Idempotency --> Claim["claim action + dispatch_attempt.started"]
  Claim --> DryRun["lark-cli messages-reply --dry-run"]
  DryRun --> DryRunOK{"dry-run ok"}
  DryRunOK -->|否| Failed["attempt failed<br/>action failed"]
  DryRunOK -->|是| ActualSend["actual messages-reply"]
  ActualSend --> SendOK{"发送成功"}
  SendOK -->|明确未发送| Failed
  SendOK -->|结果不确定| NeedsReview["attempt uncertain<br/>action failed_needs_review"]
  SendOK -->|是| RecordSent["记录 sent_message_id"]
  RecordSent --> ReadBack["messages-mget 读回验证"]
  ReadBack --> Verify{"reply_to 和 mentions 符合预期"}
  Verify -->|否| VerifyWarn["attempt send_ok<br/>记录 warning / 人工审计"]
  Verify -->|是| Done["action sent"]
  VerifyWarn --> Done
```

stale `sending` 不自动重发；下次 dispatch 检测到超时 in-flight action 时，将 action 标记为 `failed_needs_review`，并保留原 idempotency key 供本地恢复 CLI 人工处理。`failed_needs_review` 继续占用 active-send 约束，直到 operator `mark-sent`、`retry` 复用原 action/idempotency key，或 `cancel` 释放约束。
