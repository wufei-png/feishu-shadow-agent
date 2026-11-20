export type RouteKey = "dashboard" | "coworker" | "approvals" | "tasks" | "dispatch" | "feedback" | "policy" | "settings" | "health" | "maintenance";

export type CoworkerPolicy = {
  enabled: boolean;
  chat_id: string | null;
  provider: string;
  send_mode: string;
  trigger_mode: "mention" | "followups";
  history_source: "live_on_demand" | "local_cache";
  memory_maintenance: boolean;
  memory_maintenance_time?: string;
};

export type NativeCoworkerSession = {
  session_id: number | null;
  app_id: string;
  chat_id: string;
  topic_key: string;
  deliveries: Array<{
    id: number;
    purpose: string;
    target_message_id: string;
    sent_message_id: string | null;
    receipt_thread_id: string | null;
    error: string | null;
    updated_at: string;
  }>;
  handoff: {
    status: string;
    error: string | null;
    updated_at: string;
    operator_command: string | null;
  } | null;
  watch_expires_at: string | null;
};

export type CoworkerSnapshot = {
  available: boolean;
  runtime_mode?: "production" | "dry_run";
  config: CoworkerPolicy | null;
  policy: CoworkerPolicy | null;
  policy_invalid?: boolean;
  runtime: {
    business_mode: string;
    send_mode: string | null;
    chat_id: string | null;
    provider: string | null;
    coworker_runtime?: "native" | "legacy" | null;
    reply_mode?: "quote" | "topic" | "auto" | null;
  } | null;
  liveness: { status: string };
  memory_maintenance: { state?: string; last_attempt?: string; last_success_at?: string; memory_file_changed?: boolean | null; error_type?: string | null } | null;
  backend_health?: { status: string; message: string; checked_at: string } | null;
  sessions: Array<{ id: number; thread_id: string; state: string; provider: string }>;
  events: Array<{ id: number; topic_key: string; message_id: string; status: string; error: string | null }>;
  subagents: Array<{ child_thread_id: string; status: string; outcome: string | null }>;
  history_gaps: Array<{ topic_key: string; needs_backfill: number; has_more: number; last_error: string | null }>;
  delegations: Array<{ id: string; peer_id: string; status: string; deadline: string | null }>;
  native_sessions?: NativeCoworkerSession[];
};

export type Tone = "neutral" | "success" | "warning" | "danger" | "info" | "muted";

export type CommandResult = {
  status: string;
  command: string;
  actor: string;
  reason: string | null;
  target: Record<string, unknown>;
  changed: boolean;
  result: Record<string, unknown>;
  warnings: string[];
  next_actions: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export type DashboardSnapshot = {
  runtime_mode?: "production" | "dry_run";
  daemon_liveness?: {
    status?: string;
    run_status?: string;
    last_tick_started_at?: string | null;
    last_tick_finished_at?: string | null;
    [key: string]: unknown;
  };
  policy_status?: PolicyStatus;
  store_status?: { status?: string; available?: boolean; schema_initialized?: boolean };
  health_issue_summary?: HealthIssueSummary;
  attention_summary?: AttentionSummary;
  attention_tasks?: AttentionTask[];
  ingestion_status?: IngestionStatus;
  bot_membership_status?: BotMembershipStatus;
  pending_approvals?: ApprovalSummary[];
  active_tasks?: TaskSummary[];
  pending_actions?: DispatchActionSummary[];
  failed_or_needs_review_actions?: DispatchActionSummary[];
  stale_sending_actions?: DispatchActionSummary[];
  recent_health_warnings?: Array<Record<string, unknown>>;
  recent_errors?: Array<Record<string, unknown>>;
  failed_approval_commands?: Array<Record<string, unknown>>;
  has_messages?: boolean | null;
  last_run?: {
    last_tick_started_at?: string | null;
    last_tick_finished_at?: string | null;
  } | null;
};

export type BotMembershipStatus = {
  summary: {
    present: number;
    absent: number;
    unknown: number;
    unobserved: number;
  };
  facts: Array<{
    chat_id: string;
    status: "present" | "absent" | "unknown" | "unobserved";
    observed_status: string | null;
    checked_at: string | null;
    next_probe_at: string | null;
    source: string | null;
    error: string | null;
    updated_at: string | null;
  }>;
};

export type IngestionStatus = {
  summary: {
    source_count: number;
    backlog_count: number;
    budget_exhausted_count: number;
    oldest_checkpoint_age_seconds: number | null;
  };
  sources: Array<{
    checkpoint_key: string;
    updated_at: string | null;
    last_success_at: string | null;
    checkpoint_age_seconds: number | null;
    drain_complete: boolean;
    backlog: {
      start?: string;
      end?: string;
      reason?: string;
      pages_fetched?: number;
      messages_fetched?: number;
      restart_count?: number;
    } | null;
    last_drain: {
      pages_fetched?: number;
      messages_fetched?: number;
      completed_at?: string;
    } | null;
  }>;
};

export type AttentionSummary = {
  pending_approval_count: number;
  overdue_approval_count: number;
  failed_action_count: number;
  uncertain_action_count: number;
  blocked_processing_count: number;
  failed_processing_count: number;
  affected_task_count: number;
  total_item_count: number;
};

export type AttentionTask = {
  task_id: number;
  task_short_id: string;
  task_label: string | null;
  chat_id: string | null;
  status: TaskStatus;
  pending_approval_count: number;
  overdue_approval_count: number;
  failed_action_count: number;
  uncertain_action_count: number;
  blocked_processing_count: number;
  failed_processing_count: number;
  latest_at: string | null;
};

export type HealthSeverity = "info" | "warning" | "error" | "critical";

export type HealthIssueSummary = {
  highest_severity: HealthSeverity;
  open_issue_count: number;
};

export type HealthIssueLink = {
  type: "task" | "message" | "approval" | "dispatch_action" | "policy" | "settings";
  id: string;
};

export type HealthIssue = {
  id: string;
  severity: HealthSeverity;
  category: "daemon" | "policy" | "approval_command" | "dispatch" | "store" | "runtime";
  title: string;
  detail: string;
  detected_at: string | null;
  links: HealthIssueLink[];
  recommended_actions: string[];
};

export type HealthIssuesResponse = {
  generated_at: string;
  summary: HealthIssueSummary;
  runtime: {
    store?: Record<string, unknown>;
    daemon_liveness?: Record<string, unknown>;
    last_run?: Record<string, unknown> | null;
    policy_status?: PolicyStatus;
  };
  issues: HealthIssue[];
  recent_failed_commands?: Array<Record<string, unknown>>;
  recent_failed_dispatch_actions?: DispatchActionSummary[];
};

export type PolicyStatus = {
  initialized?: boolean;
  global_policy_updated_at?: string | null;
  chat_policy_count?: number;
  policy_import_diff?: PolicyImportDiff;
};

export type PolicyImportDiff = {
  status?: string;
  message?: string;
  missing_global?: boolean;
  changed_global?: boolean;
  missing_chats?: string[];
  changed_chats?: string[];
  missing_coworker?: boolean;
  changed_coworker?: boolean;
};

export type ReplyIdentity = "bot_preferred" | "bot" | "user";

export type ProductPolicy = {
  reply_policy?: {
    p2p_auto_reply?: boolean;
    unknown_group_auto_reply?: boolean;
  };
  default_chat_policy?: {
    bot_joined?: boolean;
    reply_identity?: ReplyIdentity;
    allow_user_fallback?: boolean;
    resource_download?: boolean;
  };
};

export type ChatPolicy = {
  chat_id: string;
  name: string;
  auto_reply: boolean;
  bot_joined: boolean;
  reply_identity: ReplyIdentity;
  allow_user_fallback: boolean;
  resource_download: boolean;
  updated_at?: string | null;
};

export type PolicyAudit = {
  id: number;
  scope: string;
  policy_key: string;
  actor: string;
  reason: string | null;
  created_at: string | null;
  old_summary: Record<string, unknown>;
  new_summary: Record<string, unknown>;
};

export type PolicyFieldChange = {
  field: string;
  before: unknown;
  after: unknown;
  subject?: string;
};

export type PolicyImpactPreview = {
  scope: string;
  operation: string;
  target: Record<string, unknown>;
  field_changes: PolicyFieldChange[];
  effective_before: Record<string, unknown>;
  effective_after: Record<string, unknown>;
  behavior_changes: PolicyFieldChange[];
  affected_summary: Record<string, unknown>;
  warnings: string[];
};

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

export type ApprovalSummary = {
  id: number;
  approval_id: string;
  short_id: string;
  task_id: number | null;
  task_short_id: string | null;
  source_message_id: string | null;
  source_revision: number | null;
  kind: string;
  status: ApprovalStatus;
  preview: string | null;
  created_at: string | null;
  expires_at: string | null;
  resolved_at: string | null;
  is_overdue: boolean;
  overdue_seconds: number;
  recommended_action: string;
  available_commands: string[];
  postprocess_status?: string | null;
  postprocess_applied?: boolean | null;
  postprocess_failure_reason?: string | null;
};

export type ApprovalDetail = ApprovalSummary & {
  payload?: Record<string, unknown>;
};

export type FeedbackExecutionMode = "production" | "dry_run" | "all";

export type FeedbackCount = {
  value: string;
  count: number;
  rate: number | null;
};

export type FeedbackMetrics = {
  days: number;
  since: string;
  total: number;
  suggestion_sent: number;
  edited_sent: number;
  no_send: number;
  changed_reply_count: number;
  feedback_reason_count: number;
  content_expired_count: number;
  sent_without_edit_rate: number | null;
  edit_rate_among_sends: number | null;
  no_send_rate: number | null;
  by_outcome: FeedbackCount[];
  by_decision_reason: FeedbackCount[];
  by_feedback_reason: FeedbackCount[];
};

export type ReplyDiffPart = {
  op: "equal" | "insert" | "delete" | "replace";
  before?: string;
  after?: string;
};

export type FeedbackRecord = {
  id: number;
  approval_id: string;
  task_id: string | null;
  outcome: "suggestion_sent" | "edited_sent" | "no_send_keep_watching" | "no_send_end_task";
  decision_reason: string | null;
  feedback_reason: string | null;
  note: string | null;
  actor: string;
  execution_mode: "production" | "dry_run";
  content_expired_at: string | null;
  created_at: string;
  reply_comparison: {
    status: "changed" | "unchanged" | "expired" | "not_applicable" | "unavailable";
    suggested_reply: string | null;
    final_reply: string | null;
    diff: ReplyDiffPart[];
  };
};

export type FeedbackOverview = {
  generated_at: string;
  execution_mode: FeedbackExecutionMode;
  windows: FeedbackMetrics[];
  recent: FeedbackRecord[];
};

export type TaskStatus = "watching" | "closed" | "closed_by_owner" | "human_taken_over";

export type TaskSummary = {
  id: number;
  task_id: string;
  task_short_id: string;
  short_id: string;
  status: TaskStatus;
  policy_held?: boolean;
  chat_id: string | null;
  chat_type: string | null;
  thread_id: string | null;
  root_message_id: string | null;
  task_label: string | null;
  watch_until: string | null;
  agent_working_dir: string | null;
  updated_at: string | null;
  message_count: number;
  recommended_actions: string[];
};

export type TaskMessage = {
  message_id: string;
  role: string;
  sender_role: string | null;
  sent_at: string | null;
  text: string | null;
  created_at: string | null;
};

export type EffectivePolicy = {
  policy_source: string;
  auto_reply: boolean | null;
  bot_joined: boolean | null;
  reply_identity: string | null;
  allow_user_fallback: boolean | null;
  resource_download: boolean | null;
  error?: string;
};

export type TaskDetail = TaskSummary & {
  recent_messages: TaskMessage[];
  pending_approvals: ApprovalSummary[];
  actions: DispatchActionSummary[];
  agent_audits: AgentAudit[];
  processing?: MessageProcessing[];
  task_background: TaskBackgroundVersion | null;
  task_background_history: TaskBackgroundVersion[];
  effective_policy: EffectivePolicy;
  recommended_actions: string[];
};

export type TaskBackgroundVersion = {
  id: number;
  task_id: number;
  version: number;
  content: string | null;
  operation: "set" | "clear";
  actor: string;
  reason: string | null;
  created_at: string | null;
  content_expired_at: string | null;
};

export type ActionStatus = "pending" | "sending" | "sent" | "failed" | "failed_needs_review" | "cancelled";

export type DispatchActionSummary = {
  id: number;
  action_id: number;
  kind: string;
  status: ActionStatus;
  policy_held?: boolean;
  task_id: number | null;
  task_short_id: string | null;
  approval_id: number | null;
  target_message_id: string | null;
  dry_run: boolean;
  created_at: string | null;
  updated_at: string | null;
  result_summary: Record<string, unknown>;
  recommended_actions: string[];
};

export type DispatchAttempt = {
  id: number;
  action_id: number;
  run_id: string | null;
  status: string;
  dry_run_result: unknown;
  send_result: unknown;
  readback_result: unknown;
  sent_message_id: string | null;
  error_stage: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type DispatchActionDetail = {
  action: DispatchActionSummary & {
    idempotency_key: string;
    payload: Record<string, unknown>;
    result: Record<string, unknown>;
  };
  attempts: DispatchAttempt[];
  readback_summary: Record<string, unknown>;
  recommended_actions: string[];
};

export type MessageDetail = {
  message: {
    message_id: string;
    chat_id: string | null;
    chat_type: string | null;
    sender_id: string | null;
    sender_name: string | null;
    sender_type: string | null;
    sender_role: string | null;
    sent_at: string | null;
    thread_id: string | null;
    reply_to_message_id: string | null;
    direct_mention: boolean;
    at_all: boolean;
    text: string | null;
    normalized: Record<string, unknown>;
    inserted_at: string | null;
  };
  task_ids: number[];
  task_summaries: TaskSummary[];
  routing_audits: Array<Record<string, unknown>>;
  processing: MessageProcessing[];
  resources: MessageResource[];
  agent_audits: AgentAudit[];
  approvals: ApprovalDetail[];
  actions: DispatchActionSummary[];
  recorded_dispatch_outcomes: Array<Record<string, unknown>>;
  recommended_actions: string[];
};

export type MessageProcessing = {
  id: number;
  message_id: string;
  revision: number;
  task_id: number | null;
  stage: string;
  status: string;
  attempt_count: number;
  last_error: string | null;
  terminal_reason: string | null;
  created_at: string | null;
  updated_at: string | null;
  latest_retry?: {
    id: number;
    status: "queued" | "claimed" | "succeeded" | "failed" | "cancelled";
    policy_held?: boolean;
    actor: string;
    reason: string | null;
    error: string | null;
    created_at: string | null;
    finished_at: string | null;
  } | null;
};

export type MessageResource = {
  id: number;
  message_id: string;
  file_key: string;
  resource_type: string;
  download_status: string;
  path: string | null;
  path_exists: boolean | null;
  sha256: string | null;
  sha256_short: string | null;
  created_at: string | null;
  updated_at: string | null;
  raw_summary: Record<string, unknown>;
  raw: Record<string, unknown>;
};

export type AgentAudit = {
  id: number;
  backend_provider: string;
  request_type: string;
  task_id: number | null;
  agent_session_id: string | null;
  input_message_ids: string[];
  input_resource_ids: string[];
  response_summary: Record<string, unknown>;
  response: Record<string, unknown>;
  error: string | null;
  latency_ms: number | null;
  tool_permissions_profile: string | null;
  prompt_debug: Record<string, unknown>;
  created_at: string | null;
};

export type SettingsCatalog = {
  version: number;
  entries: SettingsCatalogEntry[];
};

export type SettingsCatalogEntry = {
  key: string;
  label: string;
  description: string;
  help: string | null;
  source: string;
  scope: string;
  visibility: string;
  editable_v1: boolean | string;
  requires_restart: boolean;
  audit_behavior: string;
  write_boundary: string | null;
};

export type SettingsRuntime = {
  values: Record<string, unknown>;
  global_policy: ProductPolicy | null;
  chat_policies: ChatPolicy[];
  policy_status: PolicyStatus;
  policy_audit_history: PolicyAudit[];
};
