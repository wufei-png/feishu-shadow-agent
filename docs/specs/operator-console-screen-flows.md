# Operator Console Screen Flows

Status: draft

This document defines the final screen-level product contract for the local
Operator Console. It builds on the UI system, local API, and settings catalog
specs.

It is not a component implementation plan and does not define exact CSS tokens.

## Scope

This document answers:

- Which screens exist in the final local console.
- What each screen is for.
- Which data and commands each screen needs.
- How master-detail flows behave.
- Which states must be designed before implementation.

This document does not answer:

- Exact React component code.
- Exact local API implementation details.
- Exact visual token values.
- Binary packaging.
- Remote or multi-user behavior.

## Product Shape

The final Operator Console is a local operations workbench. It should help the
operator answer:

```text
What needs my attention?
Why is this task or message blocked?
What command can I safely run now?
What changed recently?
Is the runtime policy and daemon state healthy?
```

The console must not become:

- a raw database browser
- a raw log viewer
- a generic admin template
- a chat client
- a metrics-first analytics dashboard

## Navigation

Primary navigation is fixed:

```text
Dashboard
Approvals
Tasks
Dispatch
Policy
Settings
Logs / Health
```

The shell layout remains:

```text
Left navigation + top runtime strip + main work surface + persistent detail panel
```

The detail panel should be persistent for queue workflows so the operator can
move through items without losing list context.

## Shared Screen States

Every major screen must handle:

```text
loading
empty
error
stale data / refresh pending
command running
command success
command failure
background refresh while editing
narrow-width layout
reduced motion
```

Mutations must use command results for feedback and then refresh authoritative
read models.

Do not use optimistic UI for backend state changes. Optimistic UI is acceptable
only for local selection, expansion, and panel visibility.

## Dashboard

Purpose:

The dashboard is the default entry point and should expose the Action Queue.

Primary focus:

```text
pending approvals
overdue approvals
failed dispatch actions
stale sending actions
policy initialization/import diff
daemon liveness and health warnings
```

Avoid primary focus on:

```text
total messages
total tasks
cumulative reply counts
decorative trend charts
```

Required sections:

- Runtime strip summary.
- Action Queue summary.
- Pending/overdue approval preview.
- Dispatch recovery preview.
- Policy status and Policy Import Diff summary.
- Health warning summary.
- Recent command or audit highlight.

Primary interactions:

- Select an approval and open approval detail.
- Select a dispatch action and open dispatch detail.
- Jump to Policy when policy is uninitialized or import diff exists.
- Run maintenance expiry for overdue approvals through explicit command.

Primary API needs:

```text
GET /api/dashboard
GET /api/approvals
GET /api/dispatch/actions
GET /api/policy/status
GET /api/health/issues
POST /api/maintenance/expire-approvals
```

## Approvals

Purpose:

The Approvals screen is the primary work queue for human-reviewed replies.

Layout:

```text
approval queue list -> approval detail panel -> command result feedback
```

List fields:

```text
approval_id
task_id
task_short_id
kind
status
preview
created_at
expires_at
is_overdue
recommended_action
available_commands
```

Detail fields:

```text
approval payload
related task summary
recent task messages
effective policy
related dispatch actions
```

Commands:

```text
approve
reject
send final reply
expire overdue approvals
```

Rules:

- Overdue pending approvals remain pending in the read model until explicit
  expiry.
- Approve/reject/send must use `OperatorCommandService`.
- `send final reply` must preserve user input on failure.
- If a task has multiple pending approvals, conflict output should be shown as a
  recoverable command result, not as a broken UI state.

Primary API needs:

```text
GET /api/approvals
GET /api/approvals/{approval_id}
GET /api/tasks/{task_id}
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
POST /api/tasks/{task_id}/send
POST /api/tasks/{task_id}/close
POST /api/tasks/{task_id}/reopen
POST /api/maintenance/expire-approvals
```

## Tasks

Purpose:

The Tasks screen explains conversation/task context and lets the operator move
between task, message, approval, and dispatch detail.

Layout:

```text
task list -> task timeline/detail -> message detail drawer/panel
```

List fields:

```text
task_id
status
chat_id
chat_type
task_label
watch_until
message_count
updated_at
recommended_actions
```

Detail fields:

```text
task summary
recent_messages
pending_approvals
actions
effective_policy
recommended_actions
```

Message Detail:

Selecting a message opens `message_detail`, the read-only processing context for
one Feishu message.

Message Detail includes:

```text
message
task_ids
task_summaries
routing_audits
approvals
actions
recorded_dispatch_outcomes
recommended_actions
```

Rules:

- Message Detail must not generate a new preview.
- Message Detail must not mutate approvals, actions, attempts, or policy.
- If the operator needs fresh preview generation later, that is a separate
  command or replay workflow.

Primary API needs:

```text
GET /api/tasks
GET /api/tasks/{task_id}
GET /api/messages/{message_id}/detail
```

## Dispatch

Purpose:

The Dispatch screen handles send recovery and readback evidence.

Layout:

```text
dispatch action list -> dispatch detail/readback panel -> recovery command result
```

List fields:

```text
action_id
kind
status
task_id
task_short_id
target_message_id
updated_at
recommended_actions
```

Detail fields:

```text
action payload
attempts
readback_summary
recorded result
recommended_actions
```

Commands:

```text
retry
cancel
mark sent
```

Rules:

- Retry requeues according to existing dispatch recovery semantics. It must not
  automatically send.
- Mark sent requires readback evidence.
- Sent actions cannot be cancelled.
- Stale sending actions should show recovery guidance rather than silently
  changing state from a read view.

Primary API needs:

```text
GET /api/dispatch/actions
GET /api/dispatch/actions/{action_id}
POST /api/dispatch/actions/{action_id}/retry
POST /api/dispatch/actions/{action_id}/cancel
POST /api/dispatch/actions/{action_id}/mark-sent
```

## Policy

Purpose:

The Policy screen manages Product Policy and chat policy. It is where runtime
reply behavior changes, not where raw YAML is edited.

Layout:

```text
policy scope list -> policy editor/diff/audit panel
```

Sections:

- Product Policy status.
- Policy Import Diff.
- Global policy editor.
- Chat policy list and editor.
- Policy audit history.

Commands:

```text
import config
update global policy
update chat policy
```

Rules:

- Valid policy changes apply directly and write audit records.
- Do not use risk levels or confirmation-required policy flows.
- Field help explains meaning; it does not tell the operator what decision to
  make.
- `config.yaml.reply_policy` and `config.yaml.chats` remain Policy Import Source,
  not runtime truth.

Primary API needs:

```text
GET /api/policy/status
GET /api/policy/audits
GET /api/settings/catalog
POST /api/policy/import-config
PATCH /api/policy/global
PATCH /api/policy/chats/{chat_id}
```

## Settings

Purpose:

The Settings screen presents console-exposed settings in product language.

Ownership:

- Product Policy editing belongs in Policy.
- Settings may show policy field metadata when useful, but should not duplicate
  the full Policy editor.
- `config.yaml` fields are readonly in v1.

Sections:

```text
Normal
Advanced
Diagnostics
Hidden fields omitted by default
```

Rules:

- Do not auto-render raw Pydantic schema.
- Do not expose every config field just because it exists.
- Do not write `config.yaml` in v1.
- If a future config write path is added, it must go through a command facade and
  explicit audit behavior.

Primary API needs:

```text
GET /api/settings/catalog
GET /api/settings/runtime
```

## Logs / Health

Purpose:

Logs / Health is diagnostic. It should explain runtime problems without turning
the console into a raw log viewer.

Sections:

- Current health issues.
- Recent failed commands.
- Recent failed dispatch actions.
- Runtime liveness details.
- Links to relevant task, message, approval, or action detail.

Rules:

- Raw logs can appear in focused detail only when needed.
- Health summaries should stay actionable.
- Dashboard should show only high-signal health warnings.

Primary API needs:

```text
GET /api/health/issues
GET /api/dashboard
GET /api/dispatch/actions
```

## Recommended Implementation Phases

To preserve quality, implement the final screen set in phases:

```text
P16 Core Operator Screens:
  Dashboard, Approvals, Tasks, Message Detail, Dispatch

P17 Policy and Settings Screens:
  Policy editor, Policy Import Diff, audit history, Settings runtime/catalog

P18 Health, Logs, and Release Readiness:
  Logs / Health screen, visual QA, packaging/release workflow hardening
```

P16 should make the console useful for daily operator work. P17 should make
runtime policy editing product-grade. P18 should prepare the console for public
release artifacts.

## Verification Expectations

Every screen implementation phase should include:

- backend route tests for new API routes
- command route tests for mutations
- frontend typecheck and build
- browser or screenshot verification for changed screens
- narrow-width verification
- reduced-motion sanity check when motion is added
- `git diff --check`
