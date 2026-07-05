export type RouteKey = "dashboard" | "approvals" | "tasks" | "dispatch" | "policy" | "settings" | "health";

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
  daemon_liveness?: Record<string, unknown>;
  policy_status?: PolicyStatus;
  health_issue_summary?: HealthIssueSummary;
  pending_approvals?: ApprovalSummary[];
  active_tasks?: TaskSummary[];
  pending_actions?: DispatchActionSummary[];
  failed_or_needs_review_actions?: DispatchActionSummary[];
  stale_sending_actions?: DispatchActionSummary[];
  recent_health_warnings?: Array<Record<string, unknown>>;
  recent_errors?: Array<Record<string, unknown>>;
  failed_approval_commands?: Array<Record<string, unknown>>;
  last_run?: {
    last_tick_started_at?: string | null;
    last_tick_finished_at?: string | null;
  } | null;
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

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

export type ApprovalSummary = {
  id: number;
  approval_id: string;
  short_id: string;
  task_id: number | null;
  task_short_id: string | null;
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

export type TaskStatus = "watching" | "closed" | "closed_by_owner" | "human_taken_over";

export type TaskSummary = {
  id: number;
  task_id: string;
  task_short_id: string;
  short_id: string;
  status: TaskStatus;
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
  effective_policy: EffectivePolicy;
  recommended_actions: string[];
};

export type ActionStatus = "pending" | "sending" | "sent" | "failed" | "failed_needs_review" | "cancelled";

export type DispatchActionSummary = {
  id: number;
  action_id: number;
  kind: string;
  status: ActionStatus;
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
  approvals: ApprovalDetail[];
  actions: DispatchActionSummary[];
  recorded_dispatch_outcomes: Array<Record<string, unknown>>;
  recommended_actions: string[];
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
