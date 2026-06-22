# P4 Dispatch And CLI Hardening Plan

## Summary

- Accept all review points and the two follow-up execution details.
- Build P4 on the current green baseline: `.venv/bin/python -m pytest` is `100 passed`.
- Replace dispatch placeholder with audited, gated dispatch; keep MVP narrow: no SDK, no Web UI, no retry scheduler, no `config set`.

## Dispatch Safety

- Daemon dispatch must be fail-closed for real external replies:
  - Startup still runs full health.
  - Runtime health is refreshed at tick start using a lightweight critical-only check on a fixed interval; it must not run full doctor-style owner notification dry-run every tick.
  - Critical-only health covers config/store availability and required CLI/auth capabilities needed for read/send; if failed, skip ingestion and all actual sends until the next successful runtime health refresh.
  - If `approval_inbox` fails in a tick, forbid actual `send_reply`; preview/status warning only.
  - `approval_inbox` failure does not block actual `owner_notification` in normal daemon mode, because that path notifies owner for manual intervention.
  - Runtime critical health failure blocks both `send_reply` and `owner_notification`.
- Actual dispatch state machine:
  - Transactionally claim `pending -> sending`.
  - Reuse `actions.idempotency_key`; do not regenerate in dispatcher.
  - Run `messages-reply/messages-send --dry-run` first.
  - Dry-run failure marks action `failed`.
  - Actual send failure marks action `failed`.
  - Actual success with incomplete sent id/readback marks action `sent` with warnings, not retry.
  - `daemon --dry-run` writes preview only and leaves actions `pending`.
  - `daemon --dry-run --send-owner-notifications` may consume only `owner_notification`; external `send_reply` remains pending.

## CLI And Data Changes

- Extend `LarkCliClient`:
  - Parse dry-run output by stripping `=== Dry Run ===` before JSON parsing.
  - Add `reply_message(...)` and `get_messages(...)`.
  - `+messages-mget` builder uses `--message-ids om_1,om_2 --as user|bot --json --no-reactions`, max 50 ids.
- Define stable `actions.result_json` schema:
  - `dry_run`, `send`, `sent_message_id`, `readback`, `warnings`, `error_stage`.
  - Always write top-level `sent_message_id` when known so self-message routing can match sent replies.
  - When readback returns a sent message, upsert it and associate it with the task as an agent reply.
- Add store helpers for listing, claiming, preview recording, finishing, failed-command/status summaries, and stale `sending` detection.
- Implement `status` to show last run, pending approvals, failed `approval_commands`, active tasks, pending/sending/stale actions, recent failed actions, and recent health warnings.
- Implement local fallback commands through existing approval-command transaction path:
  - `approve <a_xxx|t_xxx>`
  - `reject <a_xxx|t_xxx>`
  - `send <t_xxx> <final reply...>`
  - Synthetic local command ids must be unique per invocation so a failed command can be retried.
  - `send` uses `argparse.REMAINDER`; optionally support stdin for exact multiline text.
- Narrow `replay` for P4:
  - `replay --message-id om_xxx --dry-run` explains current DB route/action state and previews pending dispatch.
  - It does not attempt true historical reprocessing or mutate the real DB.

## Test Plan

- Add dispatcher tests for claim semantics, dry-run banner parsing, dry-run failure, actual failure, success, sent-id extraction, readback warnings, sent-message upsert, and owner notification modes.
- Add daemon tests proving runtime critical health failure blocks all actual sends, while `approval_inbox` failure blocks only actual `send_reply` and still allows normal-mode owner notifications.
- Extend lark-cli tests for dry-run JSON cleanup and `+messages-mget` comma-separated builder/parser.
- Add CLI tests for `status`, failed `approval_commands`, unique synthetic command ids, `send` text preservation, stdin send text, and narrowed `replay`.
- Add one fake Feishu/Hermes E2E test covering approval inbox, group ingest, p2p ingest, active watch, and gated dispatch order.
- Acceptance: `.venv/bin/python -m pytest`.

## Assumptions

- Current local CLI baseline remains `lark-cli 1.0.56`.
- P3 owns reply composition and pending action creation; P4 only dispatches and audits.
- Stale `sending` actions are surfaced by `status`; automatic retry/recovery is out of P4.
