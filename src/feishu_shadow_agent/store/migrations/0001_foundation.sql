CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL UNIQUE,
  chat_id TEXT,
  chat_type TEXT,
  sender_id TEXT,
  sender_type TEXT,
  sent_at TEXT,
  normalized_json TEXT NOT NULL DEFAULT '{}',
  raw_json TEXT NOT NULL,
  inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  short_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  chat_id TEXT,
  root_message_id TEXT,
  task_label TEXT,
  hermes_session_id TEXT,
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
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,
  task_id INTEGER,
  approval_id INTEGER,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  target_message_id TEXT,
  dry_run INTEGER NOT NULL DEFAULT 1,
  payload_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
  FOREIGN KEY (approval_id) REFERENCES approvals(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS resources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  file_key TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  download_status TEXT NOT NULL,
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
  risk_level_max TEXT NOT NULL DEFAULT 'low',
  confidence_threshold REAL NOT NULL DEFAULT 0.85,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  suggestion_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
