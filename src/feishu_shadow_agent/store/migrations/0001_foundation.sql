CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

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
  text TEXT,
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

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  short_id TEXT NOT NULL UNIQUE,
  task_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('send_reply', 'tool_action')),
  status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
  payload_json TEXT NOT NULL DEFAULT '{}',
  preview TEXT,
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
  dry_run INTEGER NOT NULL DEFAULT 1,
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
  health_summary_json TEXT NOT NULL DEFAULT '{}'
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
  task_id INTEGER,
  agent_session_id TEXT,
  input_message_ids_json TEXT NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS message_processing (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  task_id INTEGER,
  stage TEXT NOT NULL CHECK (stage IN ('task_router', 'task_session', 'resource_download')),
  status TEXT NOT NULL
    CHECK (status IN ('processed', 'processing_failed_terminal', 'blocked_waiting_external')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  terminal_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (message_id, stage),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_sent ON messages(chat_id, sent_at, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to_message_id);
CREATE INDEX IF NOT EXISTS idx_tasks_active ON tasks(status, watch_until);
CREATE INDEX IF NOT EXISTS idx_tasks_chat_thread ON tasks(chat_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_routing_audits_message ON routing_audits(message_id);
CREATE INDEX IF NOT EXISTS idx_agent_audits_task ON agent_audits(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_approval_commands_status ON approval_commands(status, created_at);
CREATE INDEX IF NOT EXISTS idx_message_processing_status ON message_processing(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_message_processing_message ON message_processing(message_id, stage);
CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_action ON dispatch_attempts(action_id, started_at, id);
CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_status ON dispatch_attempts(status, finished_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_active_send_reply_target
ON actions(task_id, target_message_id)
WHERE kind = 'send_reply'
  AND status IN ('pending', 'sending', 'failed_needs_review')
  AND target_message_id IS NOT NULL;
