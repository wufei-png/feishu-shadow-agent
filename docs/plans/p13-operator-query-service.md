# P13 Operator Query Service Plan

## Summary

P13 introduces a stable read-only OperatorQueryService for CLI status and future UI pages. It should present product-level DTOs instead of exposing raw SQLite table shape or mutable Store helper behavior.

## Background

Operator Surface is moving toward a full local UI experience. The UI should not bind to raw SQLite table shapes or store helpers with hidden side effects. P13 creates the read side of that product boundary.

Prerequisite semantics:

- P11 removed approval expiry writes from read paths.
- P12a/P12b made Product Policy Store the runtime policy source.
- Policy Import Diff means comparing config import source with DB policy; it is not runtime policy divergence.

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/plans/operator-surface-outline.md`
- `docs/plans/p11-approval-expiry-boundary.md`
- `docs/plans/p12-product-policy-store.md`
- `docs/plans/p12b-policy-runtime-cutover.md`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/policy.py`
- `src/feishu_shadow_agent/dispatcher.py`

## Dependencies

- P11 must be complete.
- P12a and P12b must be complete.
- P13 must be complete before P14a/P14b if command results are expected to reuse operator DTO language.

## Goals

- Add `OperatorQueryService` as the read-only operator surface.
- Make `status` use OperatorQueryService instead of directly dumping `SQLiteStore.status_snapshot()`.
- Ensure operator reads do not advance approval expiry or perform other writes.
- Return stable DTOs for daemon liveness, tasks, approvals, actions, dispatch recovery, policy status, and policy import diff.
- Include effective policy summaries needed by UI.
- Provide focused detail queries for approval, task, dispatch action, and policy audit views.
- Keep query output made of stable primitives suitable for JSON/YAML rendering.

## Non-goals

- No Web UI or HTTP server.
- No policy mutation commands.
- No approval/dispatch mutation commands.
- No `context_access` changes.
- No broad SQLiteStore rewrite.

## Query Boundary

OperatorQueryService may:

- Read SQLite state.
- Derive overdue fields.
- Derive recommended operator actions.
- Resolve effective policy through Product Policy Store.
- Compare Policy Import Source to Product Policy Store for Policy Import Diff.
- Return redacted/safe operator DTOs.

OperatorQueryService must not:

- Expire approvals.
- Retry resources.
- Dispatch actions.
- Import or update policy.
- Modify daemon/checkpoint state.

## Suggested Queries And DTOs

Dashboard snapshot:

```text
daemon_liveness
policy_status
pending_approvals
active_tasks
pending_actions
failed_or_needs_review_actions
recent_health_warnings
recent_errors
```

Approval list/detail:

```text
approval_id
task_id
task_short_id
kind
status
preview
created_at
expires_at
resolved_at
is_overdue
overdue_seconds
recommended_action
available_commands
```

Task detail:

```text
task_id
task_short_id
status
chat_id
chat_type
task_label
watch_until
message_count
recent_messages
pending_approvals
actions
effective_policy
recommended_actions
```

Dispatch action detail:

```text
action
attempts
readback_summary
recommended_actions
```

Policy status:

```text
initialized: bool
global_policy_updated_at
chat_policy_count
policy_import_diff:
  status: matches | differs | unknown
  message
```

Do not return full `policy_audits` history in the default dashboard snapshot. Add a focused query for policy audit history if needed.

Effective policy summary for task/chat detail:

```text
policy_source
auto_reply
bot_joined
reply_identity
allow_user_fallback
resource_download
```

Policy audit history query:

```text
scope
policy_key
actor
reason
created_at
old_summary
new_summary
```

Support basic pagination/filter parameters on list/history queries:

```text
limit
cursor or offset
status
scope
chat_id
since
```

## Files To Update

- `src/feishu_shadow_agent/operator_query.py` or equivalent new module
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `docs/testing.md`
- `docs/specs/feishu-shadow-agent-mvp-design.md`
- `tests/test_cli.py`
- new focused operator query tests

## Implementation Notes

- Use time injection for overdue calculations so tests do not rely on wall-clock timing.
- QueryService should be usable with only a store, config import source path if needed for Policy Import Diff, and pure policy services. It must not need Feishu or Hermes clients.
- `status` may keep YAML output, but the data should come from OperatorQueryService DTOs.
- Keep raw payloads redacted or summarized in default dashboard output; expose focused detail views when operators need full context.

## Test Plan

- `status` no longer mutates overdue approval state.
- Dashboard snapshot includes daemon liveness and policy status.
- Pending overdue approval remains DB `pending` but DTO has `is_overdue = true`.
- Default dashboard snapshot omits full policy audit history.
- Effective policy summary is returned for relevant task/chat detail views.
- Approval detail returns available commands and recommended action.
- Task detail returns messages/actions/approvals/effective policy without writes.
- Dispatch action detail returns attempts/readback summary without mutating stale sends.
- Policy Import Diff is named as import-source comparison, not runtime drift.
- Policy audit history is available through a focused query with pagination/filtering.
- QueryService can be used without Feishu/Hermes clients.
- QueryService returns stable primitives suitable for JSON/YAML rendering.

## Handoff Notes

- Do not revive `SQLiteStore.status_snapshot()` as the UI contract if it still exposes raw table shape.
- Do not add expiry, dispatch recovery, resource retry, or policy mutation to query methods.
- Use Product Policy Store for effective policy; do not read runtime policy from `config.yaml`.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_cli.py
.venv/bin/python -m pytest -q
git diff --check
```
