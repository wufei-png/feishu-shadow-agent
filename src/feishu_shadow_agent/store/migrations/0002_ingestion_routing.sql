ALTER TABLE messages ADD COLUMN thread_id TEXT;
ALTER TABLE messages ADD COLUMN reply_to_message_id TEXT;
ALTER TABLE messages ADD COLUMN sender_role TEXT;
ALTER TABLE messages ADD COLUMN direct_mention INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN at_all INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN text TEXT;

ALTER TABLE tasks ADD COLUMN chat_type TEXT;
ALTER TABLE tasks ADD COLUMN thread_id TEXT;
ALTER TABLE tasks ADD COLUMN watch_until TEXT;
ALTER TABLE tasks ADD COLUMN last_user_message TEXT;
ALTER TABLE tasks ADD COLUMN last_agent_reply TEXT;

CREATE TABLE IF NOT EXISTS routing_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  task_id INTEGER,
  route TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_messages_chat_sent ON messages(chat_id, sent_at, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to_message_id);
CREATE INDEX IF NOT EXISTS idx_tasks_active ON tasks(status, watch_until);
CREATE INDEX IF NOT EXISTS idx_tasks_chat_thread ON tasks(chat_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_routing_audits_message ON routing_audits(message_id);
