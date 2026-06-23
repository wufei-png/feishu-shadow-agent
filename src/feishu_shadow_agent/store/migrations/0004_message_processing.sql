CREATE TABLE IF NOT EXISTS message_processing (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  task_id INTEGER,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  terminal_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (message_id, stage),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_message_processing_status
ON message_processing(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_message_processing_message
ON message_processing(message_id, stage);
