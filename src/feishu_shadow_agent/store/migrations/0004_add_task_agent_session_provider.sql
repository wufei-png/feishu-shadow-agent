ALTER TABLE tasks ADD COLUMN agent_session_provider TEXT;

UPDATE tasks
SET agent_session_provider = 'hermes'
WHERE agent_session_id IS NOT NULL
  AND agent_session_provider IS NULL;
