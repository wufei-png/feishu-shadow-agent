ALTER TABLE messages ADD COLUMN sender_name TEXT;

ALTER TABLE approvals ADD COLUMN preview TEXT;

UPDATE tasks
SET hermes_session_id = NULL
WHERE hermes_session_id LIKE 'feishu-task-%';

CREATE TABLE IF NOT EXISTS hermes_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_type TEXT NOT NULL,
  task_id INTEGER,
  hermes_session_id TEXT,
  input_message_ids_json TEXT NOT NULL DEFAULT '[]',
  input_resource_ids_json TEXT NOT NULL DEFAULT '[]',
  response_json TEXT,
  error TEXT,
  latency_ms INTEGER,
  prompt_json TEXT,
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_active_send_reply_target
ON actions(task_id, target_message_id)
WHERE kind = 'send_reply'
  AND status IN ('pending', 'sending')
  AND target_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_hermes_audits_task ON hermes_audits(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_approval_commands_status ON approval_commands(status, created_at);
