ALTER TABLE tasks RENAME COLUMN hermes_session_id TO agent_session_id;

UPDATE tasks
SET agent_session_id = NULL
WHERE agent_session_id LIKE 'feishu-task-%';

ALTER TABLE hermes_audits RENAME TO agent_audits;

ALTER TABLE agent_audits RENAME COLUMN hermes_session_id TO agent_session_id;

ALTER TABLE agent_audits ADD COLUMN backend_provider TEXT NOT NULL DEFAULT 'hermes';

DROP INDEX IF EXISTS idx_hermes_audits_task;
CREATE INDEX IF NOT EXISTS idx_agent_audits_task ON agent_audits(task_id, created_at);
