BEGIN IMMEDIATE;

PRAGMA application_id = 1179861319;
PRAGMA user_version = 13;

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL UNIQUE,
  chat_id TEXT,
  chat_type TEXT CHECK (chat_type IS NULL OR chat_type IN ('group', 'p2p')),
  sender_id TEXT,
  sender_name TEXT,
  sender_type TEXT,
  sender_role TEXT NOT NULL DEFAULT 'external_user_message'
    CHECK (sender_role IN ('external_user_message', 'owner_message', 'bot_message', 'agent_message')),
  sent_at TEXT,
  thread_id TEXT,
  reply_to_message_id TEXT,
  direct_mention INTEGER NOT NULL DEFAULT 0,
  at_all INTEGER NOT NULL DEFAULT 0,
  message_type TEXT,
  text TEXT,
  is_deleted INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  semantic_hash TEXT NOT NULL DEFAULT '',
  normalized_json TEXT NOT NULL DEFAULT '{}',
  raw_json TEXT NOT NULL,
  inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  short_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('watching', 'closed', 'closed_by_owner', 'human_taken_over')),
  chat_id TEXT,
  chat_type TEXT CHECK (chat_type IS NULL OR chat_type IN ('group', 'p2p')),
  thread_id TEXT,
  root_message_id TEXT,
  task_label TEXT,
  agent_session_id TEXT,
  agent_session_provider TEXT,
  agent_working_dir TEXT,
  watch_until TEXT,
  last_user_message TEXT,
  last_agent_reply TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS task_messages (
  task_id INTEGER NOT NULL,
  message_id TEXT NOT NULL,
  role TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, message_id),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_watch_keys (
  task_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, key),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_background_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  content TEXT,
  operation TEXT NOT NULL CHECK (operation IN ('set', 'clear')),
  actor TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  content_expired_at TEXT,
  UNIQUE (task_id, version),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  short_id TEXT NOT NULL UNIQUE,
  task_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('send_reply', 'tool_action')),
  status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
  payload_json TEXT NOT NULL DEFAULT '{}',
  preview TEXT,
  source_message_id TEXT,
  source_revision INTEGER,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  resolved_at TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,
  task_id INTEGER,
  approval_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('send_reply', 'owner_notification')),
  status TEXT NOT NULL
    CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'failed_needs_review', 'cancelled')),
  target_message_id TEXT,
  source_message_id TEXT,
  source_revision INTEGER,
  dry_run INTEGER NOT NULL DEFAULT 1,
  execution_mode TEXT NOT NULL DEFAULT 'production'
    CHECK (execution_mode IN ('dry_run', 'production')),
  payload_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
  FOREIGN KEY (approval_id) REFERENCES approvals(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS dispatch_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id INTEGER NOT NULL,
  run_id TEXT,
  claim_token TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL
    CHECK (status IN ('started', 'dry_run_ok', 'send_ok', 'readback_ok', 'failed', 'uncertain')),
  dry_run_result_json TEXT,
  send_result_json TEXT,
  readback_result_json TEXT,
  sent_message_id TEXT,
  error_stage TEXT
    CHECK (error_stage IS NULL OR error_stage IN ('claim', 'dry_run', 'send', 'readback', 'recovery')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  FOREIGN KEY (action_id) REFERENCES actions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  file_key TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  download_status TEXT NOT NULL
    CHECK (download_status IN (
      'downloaded', 'skipped', 'bot_not_joined', 'bot_invisible', 'failed',
      'missing_file', 'too_large', 'quota_exceeded', 'expired'
    )),
  path TEXT,
  sha256 TEXT,
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (message_id, file_key, resource_type)
);

CREATE TABLE IF NOT EXISTS checkpoints (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 1,
  git_commit TEXT,
  git_dirty INTEGER,
  health_summary_json TEXT NOT NULL DEFAULT '{}',
  last_heartbeat_at TEXT,
  last_tick_started_at TEXT,
  last_tick_finished_at TEXT,
  last_tick_status TEXT
    CHECK (
      last_tick_status IS NULL
      OR last_tick_status IN ('running', 'ok', 'partial_failed', 'failed')
    ),
  last_tick_summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS health_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  check_name TEXT NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  checked_at TEXT NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS chat_policies (
  chat_id TEXT PRIMARY KEY,
  name TEXT,
  auto_reply INTEGER NOT NULL DEFAULT 0,
  bot_joined INTEGER NOT NULL DEFAULT 0,
  reply_identity TEXT NOT NULL DEFAULT 'bot_preferred',
  allow_user_fallback INTEGER NOT NULL DEFAULT 1,
  resource_download INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_policy_holds (
  kind TEXT NOT NULL CHECK (kind IN ('task', 'action', 'retry')),
  item_id INTEGER NOT NULL,
  chat_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (kind, item_id)
);

CREATE TABLE IF NOT EXISTS product_policies (
  key TEXT PRIMARY KEY,
  policy_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL CHECK (scope IN ('global', 'chat')),
  policy_key TEXT NOT NULL,
  actor TEXT NOT NULL,
  old_json TEXT,
  new_json TEXT,
  reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  suggestion_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS routing_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  task_id INTEGER,
  route TEXT NOT NULL
    CHECK (route IN ('new_task', 'attach_task', 'reopen_task', 'close_task', 'ignore', 'ambiguous', 'human_taken_over')),
  route_reason TEXT,
  candidates_count INTEGER NOT NULL DEFAULT 0,
  shortcut_hit INTEGER NOT NULL DEFAULT 0,
  router_called INTEGER NOT NULL DEFAULT 0,
  matched_by TEXT,
  target_task_id INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
  FOREIGN KEY (target_task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS agent_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backend_provider TEXT NOT NULL DEFAULT 'hermes',
  request_type TEXT NOT NULL,
  prompt_version TEXT,
  prompt_hash TEXT,
  task_id INTEGER,
  agent_session_id TEXT,
  input_message_ids_json TEXT NOT NULL DEFAULT '[]',
  input_message_revisions_json TEXT NOT NULL DEFAULT '[]',
  input_resource_ids_json TEXT NOT NULL DEFAULT '[]',
  response_json TEXT,
  error TEXT,
  latency_ms INTEGER,
  prompt_json TEXT,
  tool_permissions_profile TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS approval_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL UNIQUE,
  command TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  approval_id INTEGER NOT NULL UNIQUE,
  task_id INTEGER,
  command_id TEXT NOT NULL UNIQUE,
  outcome TEXT NOT NULL
    CHECK (outcome IN (
      'suggestion_sent',
      'edited_sent',
      'no_send_keep_watching',
      'no_send_end_task'
    )),
  decision_reason TEXT,
  suggested_reply TEXT,
  final_reply TEXT,
  feedback_reason TEXT
    CHECK (feedback_reason IS NULL OR feedback_reason IN (
      'inaccurate_or_unsupported',
      'incomplete_context',
      'tone_or_style',
      'unnecessary_reply',
      'other'
    )),
  note TEXT,
  actor TEXT NOT NULL,
  execution_mode TEXT NOT NULL
    CHECK (execution_mode IN ('dry_run', 'production')),
  content_expired_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (approval_id) REFERENCES approvals(id) ON DELETE CASCADE,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS message_processing (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  task_id INTEGER,
  stage TEXT NOT NULL CHECK (stage IN ('task_router', 'task_session', 'resource_download')),
  status TEXT NOT NULL
    CHECK (status IN ('processed', 'processing_failed_terminal', 'blocked_waiting_external')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  terminal_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (message_id, revision, stage),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS processing_retry_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  task_id INTEGER,
  stage TEXT NOT NULL CHECK (stage IN ('task_router', 'task_session', 'resource_download')),
  source_status TEXT NOT NULL
    CHECK (source_status IN ('processing_failed_terminal', 'blocked_waiting_external')),
  status TEXT NOT NULL CHECK (status IN ('queued', 'claimed', 'succeeded', 'failed', 'cancelled')),
  claim_token TEXT,
  run_id TEXT,
  actor TEXT NOT NULL,
  reason TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  finished_at TEXT,
  content_expired_at TEXT,
  FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE CASCADE,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_sent ON messages(chat_id, sent_at, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to_message_id);
CREATE INDEX IF NOT EXISTS idx_tasks_active ON tasks(status, watch_until);
CREATE INDEX IF NOT EXISTS idx_tasks_chat_thread ON tasks(chat_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_task_background_versions_task
ON task_background_versions(task_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_routing_audits_message ON routing_audits(message_id);
CREATE INDEX IF NOT EXISTS idx_agent_audits_task ON agent_audits(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_approval_commands_status ON approval_commands(status, created_at);
CREATE INDEX IF NOT EXISTS idx_message_processing_status ON message_processing(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_message_processing_message ON message_processing(message_id, revision, stage);
CREATE UNIQUE INDEX IF NOT EXISTS idx_processing_retry_active
ON processing_retry_attempts(message_id, revision, stage)
WHERE status IN ('queued', 'claimed');
CREATE INDEX IF NOT EXISTS idx_processing_retry_status
ON processing_retry_attempts(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_action ON dispatch_attempts(action_id, started_at, id);
CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_status ON dispatch_attempts(status, finished_at);
CREATE INDEX IF NOT EXISTS idx_policy_audits_policy ON policy_audits(policy_key, created_at);
CREATE INDEX IF NOT EXISTS idx_approval_feedback_created_at ON approval_feedback(created_at);
CREATE INDEX IF NOT EXISTS idx_approval_feedback_outcome_reason
ON approval_feedback(outcome, decision_reason, created_at);

-- Latest-state-per-check lookups stay indexed as health_checks grows; the
-- correlated "newer row" probe otherwise degrades quadratically. The DESC keys
-- are load-bearing: they let the probe stop at the newest row for a check.
CREATE INDEX IF NOT EXISTS idx_health_checks_name_checked
ON health_checks(check_name, checked_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_actions_status_updated
ON actions(status, updated_at, id);
CREATE INDEX IF NOT EXISTS idx_approvals_status_expires
ON approvals(status, expires_at, id);

DROP INDEX IF EXISTS idx_actions_active_send_reply_target;
CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_active_send_reply_target
ON actions(
  task_id,
  target_message_id,
  execution_mode,
  COALESCE(source_message_id, ''),
  COALESCE(source_revision, 0)
)
WHERE kind = 'send_reply'
  AND status IN ('pending', 'sending', 'failed_needs_review')
  AND target_message_id IS NOT NULL;

-- coworker schema begin
CREATE TABLE IF NOT EXISTS coworker_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  backend_session_id TEXT,
  agent_working_dir TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'closed', 'unrecoverable')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (app_id, chat_id, thread_id, provider, generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_coworker_active_session
ON coworker_sessions(app_id, chat_id, thread_id, provider) WHERE state = 'active';
CREATE TABLE IF NOT EXISTS coworker_topic_aliases (
  app_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  PRIMARY KEY (app_id, chat_id, alias)
);
CREATE TABLE IF NOT EXISTS coworker_history_state (
  app_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  needs_backfill INTEGER NOT NULL DEFAULT 1,
  generation INTEGER NOT NULL DEFAULT 0,
  has_more INTEGER,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (app_id, chat_id, topic_key)
);
CREATE TABLE IF NOT EXISTS coworker_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id TEXT NOT NULL,
  event_id TEXT,
  message_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  group_activation_id INTEGER,
  runtime TEXT NOT NULL DEFAULT 'legacy' CHECK (runtime IN ('legacy', 'native')),
  revision INTEGER NOT NULL DEFAULT 1,
  raw_json TEXT,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'completed', 'needs_review', 'discarded')),
  error TEXT,
  received_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (app_id, message_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_coworker_events_status ON coworker_events(status, received_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_coworker_events_processing_topic
ON coworker_events(app_id, chat_id, topic_key) WHERE status = 'processing';
CREATE TABLE IF NOT EXISTS coworker_memory_events (
  event_id INTEGER PRIMARY KEY,
  processed_at TEXT NOT NULL,
  FOREIGN KEY (event_id) REFERENCES coworker_events(id)
);
CREATE TABLE IF NOT EXISTS coworker_delegations (
  id TEXT PRIMARY KEY,
  session_id INTEGER NOT NULL,
  peer_id TEXT NOT NULL,
  hop INTEGER NOT NULL DEFAULT 1,
  request_action_id INTEGER,
  request_approval_id INTEGER,
  request_message_id TEXT,
  result_message_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'timed_out', 'failed')),
  deadline TEXT NOT NULL,
  timeout_seconds INTEGER NOT NULL,
  timer_started INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES coworker_sessions(id),
  FOREIGN KEY (request_action_id) REFERENCES actions(id),
  FOREIGN KEY (request_approval_id) REFERENCES approvals(id)
);
CREATE INDEX IF NOT EXISTS idx_coworker_delegations_waiting
ON coworker_delegations(session_id, peer_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_coworker_one_pending_peer
ON coworker_delegations(session_id, peer_id) WHERE status = 'pending';
-- coworker background schema begin
CREATE TABLE IF NOT EXISTS coworker_subagents (
  child_thread_id TEXT PRIMARY KEY,
  parent_thread_id TEXT NOT NULL,
  agent_path TEXT NOT NULL,
  session_id INTEGER NOT NULL,
  origin_event_id INTEGER NOT NULL,
  bot_open_id TEXT NOT NULL,
  child_turn_id TEXT,
  outcome TEXT,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'needs_review')),
  owner_pid INTEGER NOT NULL,
  owner_host TEXT NOT NULL,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES coworker_sessions(id),
  FOREIGN KEY (origin_event_id) REFERENCES coworker_events(id)
);
CREATE INDEX IF NOT EXISTS idx_coworker_subagents_session_status
ON coworker_subagents(session_id, status);
-- coworker background schema end
-- coworker native schema begin
CREATE TABLE IF NOT EXISTS coworker_native_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL,
  session_id INTEGER NOT NULL,
  purpose TEXT NOT NULL DEFAULT 'reply' CHECK (purpose IN ('reply', 'topic', 'transfer')),
  source_revision INTEGER NOT NULL,
  source_message_id TEXT NOT NULL,
  source_snapshot_json TEXT,
  target_message_id TEXT NOT NULL,
  text TEXT NOT NULL,
  reply_in_thread INTEGER NOT NULL DEFAULT 0,
  requires_watch INTEGER NOT NULL DEFAULT 0,
  bot_open_id TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'sending', 'sent', 'previewed', 'needs_review', 'discarded')),
  sent_message_id TEXT,
  thread_id TEXT,
  bound_topic_key TEXT,
  error TEXT,
  owner_pid INTEGER,
  owner_host TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (event_id, purpose),
  FOREIGN KEY (event_id) REFERENCES coworker_events(id),
  FOREIGN KEY (session_id) REFERENCES coworker_sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_coworker_native_deliveries_status
ON coworker_native_deliveries(status, id);
CREATE TABLE IF NOT EXISTS coworker_native_delivery_context (
  delivery_id INTEGER NOT NULL,
  message_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  snapshot_json TEXT,
  PRIMARY KEY (delivery_id, message_id),
  FOREIGN KEY (delivery_id) REFERENCES coworker_native_deliveries(id)
);
CREATE TABLE IF NOT EXISTS coworker_native_transfers (
  event_id INTEGER PRIMARY KEY,
  delivery_id INTEGER NOT NULL,
  source_event_id INTEGER NOT NULL,
  reply_target TEXT NOT NULL,
  FOREIGN KEY (event_id) REFERENCES coworker_events(id),
  FOREIGN KEY (delivery_id) REFERENCES coworker_native_deliveries(id),
  FOREIGN KEY (source_event_id) REFERENCES coworker_events(id)
);
CREATE TABLE IF NOT EXISTS coworker_native_watches (
  app_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  owner_open_id TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (app_id, chat_id, topic_key)
);
CREATE TABLE IF NOT EXISTS coworker_native_handoffs (
  session_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('pause_requested', 'releasing', 'ready', 'resume_requested', 'reloading', 'needs_review')),
  error TEXT,
  owner_pid INTEGER,
  owner_host TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES coworker_sessions(id)
);
CREATE TABLE IF NOT EXISTS coworker_native_dry_run_sessions (
  session_id INTEGER PRIMARY KEY,
  backend_session_id TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES coworker_sessions(id)
);
CREATE TABLE IF NOT EXISTS coworker_native_steers (
  event_id INTEGER PRIMARY KEY,
  origin_event_id INTEGER NOT NULL,
  session_id INTEGER NOT NULL,
  turn_id TEXT NOT NULL,
  source_revision INTEGER NOT NULL,
  snapshot_json TEXT,
  status TEXT NOT NULL CHECK (status IN ('accepted', 'needs_review')),
  updated_at TEXT NOT NULL,
  FOREIGN KEY (event_id) REFERENCES coworker_events(id),
  FOREIGN KEY (origin_event_id) REFERENCES coworker_events(id),
  FOREIGN KEY (session_id) REFERENCES coworker_sessions(id)
);
-- coworker native schema end
-- coworker schema end

COMMIT;
