ALTER TABLE policy_audits RENAME TO policy_audits_old;

CREATE TABLE policy_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL CHECK (scope IN ('global', 'chat')),
  policy_key TEXT NOT NULL,
  actor TEXT NOT NULL,
  old_json TEXT,
  new_json TEXT,
  reason TEXT,
  created_at TEXT NOT NULL
);

INSERT INTO policy_audits(
  id, scope, policy_key, actor, old_json, new_json, reason, created_at
)
SELECT id, scope, policy_key, actor, old_json, new_json, reason, created_at
FROM policy_audits_old;

DROP TABLE policy_audits_old;

CREATE INDEX IF NOT EXISTS idx_policy_audits_policy
ON policy_audits(policy_key, created_at);
