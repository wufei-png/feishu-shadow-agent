ALTER TABLE actions ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'legacy_untrusted'
  CHECK (execution_mode IN ('dry_run', 'production', 'legacy_untrusted'));

DROP INDEX idx_actions_active_send_reply_target;

CREATE UNIQUE INDEX idx_actions_active_send_reply_target
ON actions(task_id, target_message_id, execution_mode)
WHERE kind = 'send_reply'
  AND status IN ('pending', 'sending', 'failed_needs_review')
  AND target_message_id IS NOT NULL;

CREATE TABLE approval_feedback (
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

CREATE INDEX idx_approval_feedback_created_at
ON approval_feedback(created_at);

CREATE INDEX idx_approval_feedback_outcome_reason
ON approval_feedback(outcome, decision_reason, created_at);
