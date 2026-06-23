# P3 Hermes And Approval

## Summary

P3 is a pure business-state increment. It adds Hermes CLI routing/session calls,
strict JSON validation, reply gates, `SendComposer`, approval queue processing,
owner command ingestion, and pending action creation.

P3 does not send real Feishu replies, does not send owner notifications, and
does not perform readback. P4 owns dry-run send, actual send, readback, and
dispatcher hardening.

## Scope

- `HermesConfig` defaults to `mode: cli`; HTTP fields remain optional compatibility
  fields for future use.
- New tasks start with `tasks.hermes_session_id = NULL`.
- Legacy `feishu-task-*` values are treated as uninitialized and cleared by
  migration.
- First successful Task Session saves the real Hermes stderr `session_id`.
- Follow-up Task Sessions pass `--resume <hermes_session_id>`.
- `TaskProcessingService` sits between deterministic routing and action creation.
- P2 placeholder routes call Hermes TaskRouter, then apply
  `new_task|attach_task|reopen_task|close_task|ignore|ambiguous`.
- TaskRouter and Task Session outputs are strict Pydantic models.
- Invalid JSON/schema, invalid route target, invalid `reply_target_message_id`, or
  low confidence becomes audited ambiguity plus approval/notification state.
- `hermes_audits` records request type, task/session ids, input message/resource
  ids, response JSON, error, latency, and optional full prompt.
- Gate-passed auto replies create pending `send_reply` actions.
- `approved` is reserved for owner approval commands.
- Resource gate mapping:
  - `downloaded`: usable.
  - `bot_not_joined` / `bot_invisible`: block only when Hermes says resources are
    required, and create owner notification to add the bot.
  - `failed` / `missing_file`: downgrade to approval only when resources are
    required.
  - Resource-independent replies may proceed by Hermes decision.
- `sender_name` is persisted from raw sender/name/profile fields, with sender id
  fallback.
- `SendComposer` removes Hermes-generated `<at ...>`, `@所有人`, and `@_all`.
- Group replies prepend exactly one `<at user_id="...">display</at>` for the
  reply-target sender unless that sender is owner/bot/agent.
- Approval inbox drains owner-bot P2P using
  `+chat-messages-list --as user --user-id <bot_open_id>`.
- Bot open id is parsed from `auth status --json --verify`; missing identity is a
  critical health failure.
- Approval command insert, approval status change, task status change, and pending
  action creation happen in one transaction.
- `approval_inbox` checkpoint advances only after a whole drain succeeds.
- Duplicate approval command `message_id` is a no-op.
- Active partial unique guard applies only to `send_reply(task_id,
  target_message_id)` with status `pending|sending`; it does not affect
  `owner_notification`.

## Acceptance

- `.venv/bin/python -m pytest`
- Coverage includes Hermes CLI command building, config/health, migrations,
  routing/session schema failures, reply gate/composer behavior, approval inbox
  commands, duplicate command handling, and active send action uniqueness.



## Full Plan Details

# P3 Hermes And Approval Plan

## Summary

- Implement P3 as a pure business-state increment on top of the current green P2 baseline (`52 passed`): Hermes routing/session calls, strict JSON validation, reply gates, `SendComposer`, approval queue, owner command ingestion, and pending action creation.
- Use the installed Hermes Agent CLI as the default adapter, not an undefined HTTP API. Official docs and local `hermes 0.16.0` confirm `hermes chat -q`, `--resume`, `--quiet`, and session storage. P3 stores the real Hermes-returned session id from stderr, instead of assuming a custom `feishu-task-...` session id can be assigned.
- P3 must not send real Feishu replies or owner notifications. It creates pending `send_reply` and `owner_notification` actions; P4 owns dry-run, actual send, readback, and dispatcher hardening.

## Key Changes

- Add Hermes CLI integration:
  - Extend `hermes` config with CLI fields: `mode: cli`, `path`, `timeout_seconds`, `source: feishu-shadow-agent`, optional `model/provider`, and separate `router_max_turns` / `session_max_turns`. Hermes tool arguments are derived from top-level `tool_permissions`.
  - Replace the P1 HTTP-only health check path with CLI-mode checks: `hermes --version` and `hermes status` exit code. Keep old HTTP fields only as future-compatible optional config, not the default P3 path.
  - Add `HermesClient` protocol plus `HermesCliClient`: build `hermes chat -q <prompt> -Q --source feishu-shadow-agent <permission args> --ignore-rules`, parse stdout as strict JSON, parse `session_id:` from stderr, and pass `--resume <stored_session_id>` on task follow-ups.

- Add task routing/session processing:
  - Replace P2 `router_placeholder` branches with a real stateless Hermes TaskRouter only when deterministic shortcuts fail.
  - Validate router output against `new_task | attach_task | reopen_task | close_task | ignore | ambiguous`; reject invalid `target_task_id`, low confidence, or unknown watch keys into an audited `ambiguous` result plus owner notification action.
  - After `new_task | attach_task | reopen_task`, run one Hermes Task Session turn, validate `task_label`, `task_state`, `answerability`, `confidence`, `proposed_reply`, `reply_target_message_id`, `watch_action`, `watch_extend_minutes`, `risk_level`, and `safety_notes`.
  - Record Hermes audits in a new `hermes_audits` table with request type, task id, session id, input message/resource ids, response JSON, error, latency, and full prompt only when `debug.save_full_hermes_io` is true.

- Add reply gate, composer, and pending actions:
  - Gate auto-reply on `answerability=auto_reply`, confidence threshold, risk threshold, per-chat policy, known group policy, direct mention, no missing resource evidence, valid reply target, and no forbidden mention content.
  - For group auto-reply, prefer bot identity when `bot_joined=true`; allow user fallback only when policy allows it and no bot-only resource dependency failed. P2P auto-reply uses user identity. Approved replies and `/send` use user identity.
  - Add `SendComposer` that removes Hermes-generated `<at ...>`, `@所有人`, and `@_all`; forbidden mention content downgrades auto-reply to approval. For group replies, prepend exactly one `<at user_id="...">显示名</at>` for the reply-target sender unless the sender is owner/bot.
  - Add message `sender_name` normalization/storage so mentions have a display-name source, falling back to sender id when missing.
  - Create `actions.kind=send_reply,status=pending` for approved auto-replies, with an idempotency key and a partial unique guard on active `task_id + target_message_id`.

- Add approvals and owner commands:
  - Extend approvals with preview/notification metadata and add an `approval_commands` table keyed by `message_id` to make inbox command processing idempotent.
  - `ApprovalService` creates `send_reply` approvals when gates fail or Hermes schema/target validation fails, marks task `waiting_approval`, and creates a pending `owner_notification` action with copyable `/approve`, `/send`, and `/reject` commands.
  - Replace `run_approval_inbox_placeholder` with real inbox ingestion. Resolve bot open id from `lark-cli auth status --json --verify`, then read owner-bot P2P using user identity: `+chat-messages-list --as user --user-id <bot_open_id>`.
  - Parse only owner-sent commands in that P2P: `/approve <a_id|t_id>`, `/reject <a_id|t_id>`, `/send <task_id> <final reply>`. Task-id shortcut works only when exactly one pending approval exists for the task; multiple pending approvals produce an owner notification asking for the concrete `a_...`.
  - `/approve` marks approval approved and creates one pending send action. `/reject` marks approval rejected and closes the task. `/send` creates an approved manual `send_reply` approval and pending send action, using the sole pending approval target when available, otherwise the task root message.

## Test Plan

- Add focused P3 tests for Hermes CLI command construction, stdout JSON validation, stderr session id capture, resume behavior, schema failures, invalid `reply_target_message_id`, and audit rows.
- Cover every TaskRouter route, including closed recall reopen, ambiguous owner notification, invalid target rejection, and deterministic shortcuts still bypassing Hermes.
- Cover reply gates: P2P allowed, unknown group blocked, per-chat auto-reply disabled, risk/confidence failures, missing resources, bot-not-joined fallback, and forbidden Hermes mentions.
- Cover `SendComposer`: one group mention, no P2P mention, owner/bot sender skipped, sender name fallback, and Hermes-generated `@` cleanup/downgrade.
- Cover approvals: `a_...` approve/reject, `t_...` shortcut success/conflict, `/send`, duplicate command message id, checkpoint rollback on inbox failure, and no real send during P3.
- Acceptance command remains `.venv/bin/python -m pytest`.

## Assumptions

- Hermes source basis: official [CLI Interface](https://hermes-agent.nousresearch.com/docs/user-guide/cli) and [CLI Commands Reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands), plus local `hermes --help` / `hermes chat --help`.
- `hermes_session_id` in this repo will store the real Hermes CLI session id returned by Hermes; task short ids remain `t_...`.
- P3 creates pending actions only. Any real Feishu send, dry-run send validation, and readback audit stay in P4.
- Approval inbox uses user-readable P2P with the bot open id because current `lark-cli 1.0.56` documents `--user-id` P2P resolution as user-identity only.
