# P16 Operator Console Core Screens Plan

## Summary

P16 turns the P15 console foundation into a usable operator workbench for daily
action handling. It implements the core operator screens:

```text
Dashboard
Approvals
Tasks
Message Detail
Dispatch
```

This phase should make the console useful for reviewing approvals, understanding
task/message context, and recovering dispatch actions. Policy editing, Settings
editing, and Logs / Health deep diagnostics are follow-up phases.

## Background

P15 delivered:

- local console runtime
- FastAPI app shell
- token and Host validation
- Vite/React/TypeScript scaffold
- built static asset packaging
- `/api/dashboard`
- `/api/settings/catalog`
- `/api/settings/runtime`
- `OperatorQueryService.message_detail()`
- `/api/messages/{message_id}/detail`

P16 builds on:

- `docs/specs/operator-console-ui-system.md`
- `docs/specs/operator-console-local-api.md`
- `docs/specs/operator-console-settings-catalog.md`
- `docs/specs/operator-console-screen-flows.md`

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/plans/p15-operator-console-foundation.md`
- `docs/specs/operator-console-screen-flows.md`
- `docs/specs/operator-console-ui-system.md`
- `docs/specs/operator-console-local-api.md`
- `src/feishu_shadow_agent/console_api.py`
- `src/feishu_shadow_agent/operator_query.py`
- `src/feishu_shadow_agent/operator_commands.py`
- `src/feishu_shadow_agent/dispatcher.py`
- `frontend/operator-console/src/App.tsx`
- `frontend/operator-console/src/styles.css`
- `tests/test_console_api.py`
- `tests/test_operator_query.py`

## Dependencies

- P15 must be complete.
- `OperatorQueryService` list/detail methods for approvals, tasks, dispatch, and
  message detail must remain read-only.
- `OperatorCommandService` must remain the only mutation boundary for approvals,
  dispatch recovery, and maintenance expiry.

## Goals

### Backend API

Add read routes:

```text
GET /api/approvals
GET /api/approvals/{approval_id}
GET /api/tasks
GET /api/tasks/{task_id}
GET /api/dispatch/actions
GET /api/dispatch/actions/{action_id}
```

Add command routes:

```text
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
POST /api/tasks/{task_id}/send
POST /api/maintenance/expire-approvals
POST /api/dispatch/actions/{action_id}/retry
POST /api/dispatch/actions/{action_id}/cancel
POST /api/dispatch/actions/{action_id}/mark-sent
```

Route handlers must be thin:

```text
request -> validation/security -> OperatorQueryService/OperatorCommandService -> JSON
```

Do not query SQLite directly from route handlers.

### Frontend Screens

Replace foundation placeholders with real screens:

- Dashboard with Action Queue previews.
- Approvals list and detail panel.
- Tasks list, task detail, and Message Detail panel.
- Dispatch action list and readback/recovery panel.

Add shared UI behavior:

- master-detail selection state
- loading, empty, error states
- command result toasts or inline result surface
- TanStack Query invalidation after mutations
- preserved form input on mutation failure
- narrow-width layout handling

### Frontend Module Boundaries

P16 must split the P15 foundation shell before adding the core screens. The
minimum frontend structure is:

```text
frontend/operator-console/src/api.ts
frontend/operator-console/src/types.ts
frontend/operator-console/src/queryKeys.ts
frontend/operator-console/src/components/
frontend/operator-console/src/screens/DashboardScreen.tsx
frontend/operator-console/src/screens/ApprovalsScreen.tsx
frontend/operator-console/src/screens/TasksScreen.tsx
frontend/operator-console/src/screens/DispatchScreen.tsx
frontend/operator-console/src/screens/MessageDetailPanel.tsx
```

`App.tsx` should keep app shell, navigation, token bootstrap, and route/screen
selection only. It should not own route-specific query logic, command mutation
logic, or detail-panel state for every screen.

### Visual Quality

Keep the UI aligned with the existing system:

- dark-first, semantic CSS tokens
- no generic admin template
- no decorative charts
- status color only for state/action feedback
- subtle motion only for list/detail transitions and command feedback
- use lucide icons selectively for navigation, commands, statuses, and field help

## Non-goals

- No full Policy editor.
- No Policy Import Diff implementation beyond existing dashboard/status summary.
- No Settings editor.
- No `config.yaml` write path.
- No Logs / Health deep screen.
- No release workflow changes.
- No Electron/Tauri or binary packaging.
- No remote access.
- No risk confirmation workflow.
- No direct SQLite reads from renderer or route handlers.

## Recommended File Scope

Backend:

```text
src/feishu_shadow_agent/console_api.py
src/feishu_shadow_agent/operator_query.py
tests/test_console_api.py
tests/test_operator_query.py
```

Frontend:

```text
frontend/operator-console/src/api.ts
frontend/operator-console/src/types.ts
frontend/operator-console/src/queryKeys.ts
frontend/operator-console/src/components/
frontend/operator-console/src/screens/
frontend/operator-console/src/App.tsx
frontend/operator-console/src/styles.css
```

Docs:

```text
docs/testing.md
docs/specs/operator-console-screen-flows.md
```

Only update docs if implementation changes the screen contract.

## Implementation Notes

### API Route Semantics

Read route query parameters should match the existing query-service shape:

```text
limit
offset
status
chat_id
```

Route-specific validation:

```text
GET /api/approvals:
  limit: integer, 1-100, default 20
  offset: integer, >=0, default 0
  status: optional single ApprovalStatus value

GET /api/tasks:
  limit: integer, 1-100, default 20
  offset: integer, >=0, default 0
  status: optional single task status
  chat_id: optional exact chat id

GET /api/dispatch/actions:
  limit: integer, 1-100, default 20
  offset: integer, >=0, default 0
  status: optional repeated ActionStatus value
```

Invalid enum values, invalid limits, and invalid offsets must return the standard
`validation_failed` error envelope. Route handlers should normalize filters
before calling `OperatorQueryService`.

Command request bodies may include:

```text
reason
command_id
final_reply
sent_message_id
```

Use `actor="local_console"` for all command routes.

All command responses return `CommandResult.as_dict()`.

### Dispatch Mark Sent

`mark-sent` is required in P16. It requires readback evidence and must use
`OperatorCommandService.mark_dispatch_sent()`.

The console API should construct the same dispatcher/readback marker used by the
CLI, using the loaded config and local Feishu client. If the readback marker
cannot be constructed, the route must return a structured command result or
standard error that the UI can render. Do not fake mark-sent in the renderer and
do not provide a UI-only disabled TODO as the final P16 behavior.

### Dashboard

The dashboard should be actionable:

- show pending/overdue approval preview
- show dispatch recovery preview
- show policy initialization/import diff summary
- show health warning count
- show recent command or audit highlights
- link or select into approval/task/dispatch detail

Avoid adding decorative trend charts or low-value totals.

### Approvals

The Approvals screen should support:

- filter by pending/expired/resolved status
- select approval
- show preview and payload in detail
- show related task/message context
- approve/reject
- send final reply for task
- expire overdue approvals

If a command returns `conflict`, show the structured conflict result and keep the
operator in context.

### Tasks And Message Detail

The Tasks screen should support:

- task list
- task detail timeline
- related approvals and actions
- effective policy summary
- open `message_detail` for any listed message

`message_detail` must stay read-only and must not generate dispatch previews.

### Dispatch

The Dispatch screen should support:

- filter by pending/sending/failed/failed_needs_review/sent/cancelled
- action detail
- attempts/readback summary
- retry
- cancel
- mark sent when evidence is provided

Retry must requeue only. It must not automatically send.

### Query Invalidation

After approval commands, invalidate:

```text
dashboard
approvals
tasks
dispatch-actions
message-detail
```

After dispatch commands, invalidate:

```text
dashboard
dispatch-actions
tasks
message-detail
```

After maintenance expiry, invalidate:

```text
dashboard
approvals
tasks
message-detail
```

## Test Plan

Backend focused tests:

```bash
.venv/bin/python -m pytest -q tests/test_console_api.py tests/test_operator_query.py tests/test_operator_commands.py
```

Full backend tests:

```bash
.venv/bin/python -m pytest -q
```

Frontend validation:

```bash
npm --prefix frontend/operator-console run build
```

Whitespace:

```bash
git diff --check
```

Browser verification:

- start the console server with a seeded fixture or local config
- verify Dashboard loads without console errors and shows Action Queue plus recent
  command or audit highlights
- verify Approvals empty/loading/error states
- verify pending approval approve/reject/send command result rendering
- verify multiple pending approvals or conflict responses keep the operator in
  context
- verify mutation failure preserves `final_reply` or `reason` input
- verify Tasks and Message Detail narrow-width behavior
- verify Message Detail displays routing, approvals, actions, and recorded
  dispatch outcomes without generating previews
- verify failed dispatch retry/cancel/mark-sent command result rendering
- verify dispatch mutation invalidates dashboard, dispatch action detail, task
  detail, and message detail query groups

## Acceptance

- Dashboard is no longer a placeholder and can navigate to core queue detail.
- Approvals screen supports list/detail and approve/reject/send command flows.
- Tasks screen supports list/detail and opens Message Detail.
- Message Detail displays routing, approvals, actions, and recorded dispatch
  outcomes without mutation.
- Dispatch screen supports list/detail and retry/cancel/mark-sent flows, or
  returns structured backend errors when readback evidence is invalid.
- All new API routes require bearer token and validate Host as in P15.
- Route handlers use QueryService/CommandService boundaries.
- Command responses are rendered from backend command result shape.
- Mutation failures preserve user input.
- Query invalidation is owned by shared query keys rather than ad hoc string
  literals per screen.
- UI handles loading, empty, error, and command-running states.
- Narrow-width layout remains usable.
- Frontend build passes.
- Relevant backend tests pass.
- `git diff --check` passes.

## Handoff Notes

- P16 should produce a useful daily operator workflow, not every final screen.
- Policy editing and Settings editing belong to P17.
- Logs / Health and release polish belong to P18.
- Do not use direct SQLite access in route handlers or renderer code.
- Do not reintroduce risk confirmation.
- Do not write `config.yaml`.
- Do not make the console remote-capable by accident.
