# P11 Approval Expiry Boundary Plan

## Summary

P11 removes hidden write side effects from operator queries. Approval expiry remains code-owned, but it must be advanced only by daemon/command/maintenance paths, not by status, replay, dispatch inspect, or future UI polling reads.

## Background

Current code has read-looking paths that advance approval expiry:

- `SQLiteStore.status_snapshot()` calls `expire_pending_approvals()`.
- `SQLiteStore.replay_summary()` calls `expire_pending_approvals()`.
- CLI `status` renders `store.status_snapshot()` directly.
- CLI `replay` copies the DB into a temporary DB before replay, so real DB mutation is avoided there today, but the store helper itself still has write behavior and must not become the future query primitive.

P11 exists so P13 can build a read-only OperatorQueryService without inheriting these side effects.

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/plans/operator-surface-outline.md`
- `docs/plans/p6-state-lifecycle.md`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/daemon.py`

## Dependencies

- P10 is recommended first for documentation clarity, but P11 can be implemented independently.
- P11 must be complete before P13.
- P11 does not depend on Product Policy Store work.

## Goals

- Stop `status` / operator query paths from mutating approval state.
- Stop `replay` from mutating approval state.
- Keep approval command handling responsible for expiring stale pending approvals before applying owner commands.
- Add an explicit maintenance command for approval expiry.
- Make read models show overdue pending approvals without changing their DB status.

## Non-goals

- No Product Policy Store changes.
- No UI pages.
- No change to approval expiry semantics once expiry is explicitly advanced.
- No agent prompt changes.

## Expiry Write Entrypoints

Allowed write entrypoints:

```text
daemon tick start or before approval inbox processing
approval command handling before approve/reject/send
explicit maintenance command such as:
  python -m feishu_shadow_agent maintenance expire-approvals --config config.yaml
```

Disallowed write entrypoints:

```text
status
OperatorQueryService
replay
dispatch inspect
UI polling/read endpoints
```

## Read Model Semantics

When an approval is past `expires_at` but expiry has not been explicitly advanced:

```text
approval.status = pending
is_overdue = true
overdue_seconds = derived from now - expires_at
recommended_action = expire | review
```

Do not show it as `expired` until DB state is actually advanced.

## Files To Update

- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/daemon.py`
- `docs/testing.md`
- `docs/configuration.md`
- `tests/test_cli.py`
- `tests/test_daemon.py`
- `tests/test_p3_hermes_approval.py`

## Implementation Notes

- Prefer adding read-only store helpers or a `mutate_expiry: bool = False` split over keeping expiry inside `status_snapshot()` / `replay_summary()`.
- Keep `apply_approval_command()` behavior: owner approve/reject/send should still expire stale pending approvals before resolving the command.
- Add the explicit maintenance command in P11, even though P14a later wraps it behind `OperatorCommandService`.
- If daemon tick advances expiry, do it at tick start or before approval inbox processing so stale approvals are not acted on as active blockers.

## Test Plan

- `status` does not update pending overdue approvals to `expired`.
- `replay` does not update pending overdue approvals to `expired`.
- `dispatch inspect` does not update pending overdue approvals to `expired`.
- Approval command processing still expires overdue approvals before resolving commands.
- Daemon tick advances approval expiry before owner command processing or task work.
- Explicit maintenance command expires overdue approvals and reports count.
- Read model or status output can show `is_overdue` while DB status remains `pending`.

## Handoff Notes

- Overdue pending approval in a read model is still `status = pending` plus derived `is_overdue`.
- Do not insert synthetic messages, close tasks, or call task-session when expiry advances.
- Do not make QueryService responsible for expiry later; expiry remains a command/daemon/maintenance mutation.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_cli.py tests/test_daemon.py tests/test_p3_hermes_approval.py
.venv/bin/python -m pytest -q
git diff --check
```
