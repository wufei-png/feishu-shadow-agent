# P7 Dispatch Recovery Plan

## Summary

P7 makes dispatch crash recovery explicit. It adds dispatch attempts, claim tokens, stale `sending` detection, and local CLI recovery commands. It must not automatically resend an action when the system cannot prove whether a real Feishu send happened.

## Goals

- Add first-class dispatch attempt records.
- Give each actual dispatch claim a `claim_token`.
- Make stale `sending` recoverable without manual SQL edits.
- Preserve idempotency keys across retry.
- Add local CLI inspect/mark-sent/retry/cancel operations.

## Non-goals

- No automatic real resend from uncertain state.
- No owner bot DM recovery commands.
- No Web UI/TUI.
- No broad `Dispatcher` rewrite beyond recovery boundary.
- No provider/backend abstraction work.

## Schema

Add table:

```text
dispatch_attempts
  id INTEGER PRIMARY KEY
  action_id INTEGER NOT NULL
  run_id TEXT
  claim_token TEXT NOT NULL UNIQUE
  status TEXT NOT NULL CHECK (status IN (
    'started',
    'dry_run_ok',
    'send_ok',
    'readback_ok',
    'failed',
    'uncertain'
  ))
  dry_run_result_json TEXT
  send_result_json TEXT
  readback_result_json TEXT
  sent_message_id TEXT
  error_stage TEXT
  started_at TEXT NOT NULL
  finished_at TEXT
```

Add matching Python enum values:

```text
DispatchAttemptStatus:
  started
  dry_run_ok
  send_ok
  readback_ok
  failed
  uncertain
```

The DB `CHECK` allowlist and Python enum must stay aligned through tests. If `error_stage` becomes a state/stage field rather than free-form diagnostic text, make it an allowlisted enum too:

```text
DispatchErrorStage:
  claim
  dry_run
  send
  readback
  recovery
```

Action status allowlist must include:

```text
failed_needs_review
```

Optional action fields if useful:

```text
claim_token
claimed_at
```

The implementation can keep claim token only on `dispatch_attempts` if store helpers can still map current sending action to the latest started attempt.

## Dispatch Flow

Actual dispatch:

```text
pending action
  -> create dispatch_attempt(status=started, claim_token=...)
  -> action.status = sending
  -> dry-run
  -> attempt.status = dry_run_ok
  -> actual send
  -> attempt.status = send_ok
  -> readback
  -> attempt.status = readback_ok when verified
  -> action.status = sent
```

Failure:

```text
dry-run failure -> attempt failed, action failed
known pre-send/send-rejected failure -> attempt failed, action failed
ambiguous failure after crossing the real send boundary -> attempt uncertain, action failed_needs_review
readback failure after send -> action sent with warning if sent_message_id is known
```

Plain `failed` means there is evidence the real message was not sent and retry is safe. Never collapse an ambiguous send outcome into plain `failed`.

Crash/uncertain:

```text
action.status = sending
attempt not finished
updated_at older than stale threshold
```

Stale detection should mark:

```text
action.status = failed_needs_review
attempt.status = uncertain
```

unless existing attempt evidence can prove the action was sent and read back.

## Manual Recovery CLI

Add subcommands:

```bash
python -m feishu_shadow_agent dispatch inspect --action-id <id>
python -m feishu_shadow_agent dispatch mark-sent --action-id <id> --sent-message-id <om_xxx>
python -m feishu_shadow_agent dispatch retry --action-id <id>
python -m feishu_shadow_agent dispatch cancel --action-id <id>
```

Semantics:

```text
inspect:
  read-only
  show action payload/result, current status, attempts, sent_message_id/readback evidence

mark-sent:
  require sent_message_id
  perform readback verification when possible
  mark action sent only with evidence

retry:
  only allowed for failed or failed_needs_review
  requeue same action as pending
  preserve original idempotency_key
  do not allow retry from sending

cancel:
  mark action cancelled
  release active send uniqueness constraint
```

No bot DM command in P7. Recovery requires local CLI because it needs full attempt evidence.

## Store Helpers

Add focused helpers rather than raw SQL in CLI:

- create dispatch attempt and claim action atomically
- update attempt stage/result
- finish action with attempt result
- list attempts for action
- find stale sending actions
- mark stale sending as `failed_needs_review`
- retry failed action
- cancel action
- mark action sent after evidence

## Safety Rules

- Never auto-retry uncertain `sending`.
- Preserve idempotency key across retry.
- `retry` may re-run dry-run and actual send only after operator action.
- If readback proves sent, prefer marking sent over retry.
- If evidence is ambiguous, status must stay `failed_needs_review`.

## Files To Update

- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/store/migrations/*.sql`
- `src/feishu_shadow_agent/dispatcher.py`
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/types.py`
- `docs/testing.md`
- `docs/specs/feishu-shadow-agent-mvp-design.md`
- `tests/test_dispatcher.py`
- `tests/test_cli.py`
- `tests/test_store_migrations.py`
- `tests/test_daemon.py`

## Test Plan

- Claim creates a dispatch attempt and moves action to `sending`.
- Dry-run failure records attempt and action failure.
- Known pre-send or send-rejected failure records attempt and action failure.
- Ambiguous post-send failure records `attempt.status = uncertain` and `action.status = failed_needs_review`.
- Successful send records `sent_message_id` in attempt and action result.
- Readback success marks attempt `readback_ok` and action `sent`.
- Stale `sending` without proof becomes `failed_needs_review`.
- `dispatch inspect` is read-only.
- `dispatch retry` only accepts `failed` and `failed_needs_review`, preserves idempotency key, and requeues action.
- `dispatch cancel` releases active-send uniqueness for that task/target.
- `dispatch mark-sent` requires `sent_message_id` and records evidence.
- No test should prove automatic resend from uncertain state, because that must not exist.
- Python `DispatchAttemptStatus` enum values match DB `CHECK` values.
- Invalid dispatch attempt status fails DB `CHECK`.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_dispatcher.py tests/test_cli.py tests/test_store_migrations.py tests/test_daemon.py
.venv/bin/python -m pytest -q
git diff --check
```
