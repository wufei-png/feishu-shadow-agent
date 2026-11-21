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

Primary navigation is fixed, and it is grouped into three sections rather than
presented as one flat list, because the grouping states what each destination is
for:

```text
Duty        Dashboard, AI Coworker, Approvals, Tasks, Dispatch, Feedback
Configure   Policy, Settings
System      Health, Maintenance
```

At 900px and below, the pinned top bar gives access to the same grouped
destinations through a menu.

The shell layout remains:

```text
Left navigation + top runtime strip + main work surface + persistent detail panel
```

The detail panel should be persistent for queue workflows so the operator can
move through items without losing list context.

Maintenance is a first-level local operations screen for explicit low-frequency
commands such as doctor, config validation, retention pruning, and reply style
refresh. It must not become a miscellaneous command drawer. Commands whose
operator context is a queue or task workflow, such as expiring approvals, remain
on the workflow screen even if their backend API namespace is maintenance.

The runtime strip reports and does not control. It carries the four live readings
(daemon liveness, policy initialization, policy import diff, data freshness) and
nothing else; operator preferences live in Settings. Each reading is a tell-tale
lamp plus its value in text, and the lamp is decorative reinforcement of what the
words already say, so it stays out of the accessibility tree.

## Materials, Motion, and Shell Behaviour

Clear Glass and Graphite share the same layout and both support light and dark
color schemes. Clear Glass uses a lightly tinted backdrop, directional
reflections, and a soft scroll edge above the pinned surface. Chromium refracts
the backdrop along the bevel. Graphite uses an opaque fill. Reduced transparency,
increased contrast, forced colors, and browsers without backdrop filtering also
get an opaque fill.

On desktop the runtime capsule floats over scrolling content. The sidebar has
its own tinted background and reflected edge. At 900px and below only the
navigation bar remains pinned and the runtime summary scrolls in the page.
Navigation and filter selection indicators, buttons, and dashboard shortcuts
share a smaller highlight. Menus and tooltips use a higher-opacity fill for
reading. Evidence, drafts, and form fields use opaque fills at every width.

Motion expresses spatial continuity. The animation library owns panel and route
transitions. Selection indicators use the positions of their options to slide.
CSS owns hover, press, colour, and focus.

```text
Route change        140ms opacity fade, no travel, no exit animation
Settings group      180ms cross-fade in, 80ms out, no overlap between grids
Detail panel        200ms, 6px rise, keyed on the selected item
List row / result   fade in with a 4px rise on mount
Selection           Damped spring between options sharing an edge in the same group
                    The spring carries its velocity into an interrupted slide
                    Glass stretches and lights the rim in proportion to its speed
```

A route change does not animate its exit. Holding the outgoing screen for even
100ms makes a high-frequency sidebar click feel slow, and a true cross-fade would
require overlapping both screens in the layout. Under `prefers-reduced-motion` the
library drops transforms and keeps opacity, so the state change is still reported
without movement. Selections across a gap or between groups update in place.

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
only for local selection, expansion, and panel visibility. Concretely: clearing a
task's owner background drops the local draft instead of blanking it, so the field
falls back to what the server actually stores and a failed command cannot look
like a successful one.

Every command must be able to say it failed. A mutation that surfaces a command
result has to build a failure result when the request never reached the daemon,
using the shared `failedCommandResult` helper. A result panel that stays empty on
failure reads as "nothing happened" rather than "this failed".

Destructive commands confirm in place. The first click arms the action and states
exactly what will happen, the second commits, and an armed button disarms itself
after a few seconds so a half-pressed destructive command cannot sit waiting on
screen. Escape cancels. Do not use `window.confirm`: it blocks the page, cannot be
styled or tested, and reports nothing when the operator cancels.

Headings carry structure, not emphasis. One `h1` per view; empty, loading, and
error states are `h2`; readouts nested inside a panel are `h3`.

## Dashboard

Purpose:

The dashboard is the default entry point and should expose the Action Queue.

Primary focus:

```text
failed and stale dispatch actions
pending and overdue approvals
blocked or failed tasks
policy initialization/import diff
daemon liveness and open Health issues
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
- Compact Action Queue with counters that filter a single list. The list can
  group the same entries by task. Each filtered view links to its full queue.
- A next-step panel when policy is missing, startup failed, daemon state needs attention, or a running daemon has not recorded its first message. Pending work remains visible alongside this guidance.
- Secondary context area with Policy Import Diff, health, and recent signals.

Primary interactions:

- Select an approval and open approval detail.
- Select a dispatch action and open dispatch detail.
- Jump to Policy when policy is uninitialized or import diff exists.
- Open the approval queue for explicit overdue expiry.

Primary API needs:

```text
GET /api/dashboard
GET /api/approvals
GET /api/dispatch/actions
GET /api/policy/status
GET /api/health/issues
```

## Approvals

Purpose:

The Approvals screen is the primary work queue for human-reviewed replies.

Layout:

Desktop fits a narrow, persistent queue alongside a wider review panel. On
compact screens, selecting an item or following an approval link opens the review
panel. Back to queue returns to the current list with the draft still attached to
its approval.

The review panel shows the suggested reply and recent related messages, then the
reply destination, actions, and command result. Technical details can be expanded
in place.

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
- The approval queue toolbar expires every overdue pending approval across
  filters and pages. It states the scope before execution and shows the count
  returned by the command beside the toolbar.
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

Desktop keeps two columns. Message evidence replaces task detail in the right
column and keeps the task list visible. Compact screens show one step at a
time. Returning restores the originating row or timeline position. Technical
fields, message evidence, and audits remain available in named disclosures.

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
processing
task_background
task_background_history
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
processing
approvals
actions
recorded_dispatch_outcomes
recommended_actions
```

Rules:

- Task background supports save, replace, and clear through an explicit
  Operator Command. The editor states that changes apply on the next fresh
  rebuild and do not enter the current live provider session.
- Message Detail must not generate a new preview.
- Loading Message Detail must not mutate approvals, actions, attempts, or policy.
- Terminal or externally blocked processing rows may expose an explicit retry
  command. Queued or claimed attempts are disabled and show their current state.
- If the operator needs fresh preview generation later, that is a separate
  command or replay workflow.

Primary API needs:

```text
GET /api/tasks
GET /api/tasks/{task_id}
PATCH /api/tasks/{task_id}/background
GET /api/messages/{message_id}/detail
POST /api/messages/{message_id}/processing/{stage}/retry
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
delete chat policy
preview global policy impact
preview chat policy impact
preview chat policy deletion fallback
```

Rules:

- Valid policy changes apply directly and write audit records.
- Do not use risk levels or confirmation-required policy flows.
- Preview routes are read-only deterministic impact views. They show field
  changes, effective before/after behavior, behavior changes, and affected
  summaries, but they do not write audit records.
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
DELETE /api/policy/chats/{chat_id}
POST /api/policy/global/preview
POST /api/policy/chats/{chat_id}/preview
POST /api/policy/chats/{chat_id}/delete-preview
```

## Settings

Purpose:

The Settings screen owns the operator's own preferences and presents
console-exposed product fields in product language.

Ownership:

- Product Policy editing belongs in Policy.
- Settings may show policy field metadata when useful, but should not duplicate
  the full Policy editor.
- `config.yaml` fields are readonly in v1.

Sections:

```text
Local preferences (appearance, language)   always visible, above the catalog
Normal
Advanced
Policy fields
Diagnostics
Hidden fields omitted by default
```

The two layers are ordered deliberately. Local preferences are the only editable
controls on the page, and they sit above the catalog rather than inside it: a
preference is not a product field, it is never searched or filtered, and it must
stay reachable while the catalog is filtered down to nothing.

Policy-owned fields are collected into their own group. Grouping by visibility
alone left the default group 69% policy-owned: the operator landed on a screen of
cards they could not act on, most of which only said "edit this elsewhere". The
policy group is not a second editor; it is the part of the index whose write path
lives on the Policy screen.

Rules:

- Do not auto-render raw Pydantic schema.
- Do not expose every config field just because it exists.
- Do not write `config.yaml` in v1.
- If a future config write path is added, it must go through a command facade and
  explicit audit behavior.
- A catalog card whose only content is "edit this elsewhere" is a dead end. If a
  field is owned by another screen, the card links to that screen.
- A card that claims to show a current value must show a value. Fields that are
  set per chat report that they are per-chat rather than reporting a count of
  chats as if it were the value.
- Chinese catalog copy is a frontend override of the API's English source copy, so
  it is scoped to Chinese. English mode reads the API copy directly; the console
  must never render Chinese product copy in an English session.

Primary API needs:

```text
GET /api/settings/catalog
GET /api/settings/runtime
```

## Health

Purpose:

Health is diagnostic. It should explain runtime problems without turning
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
- Open issue counts should reflect current unresolved health, policy, daemon,
  store, and dispatch recovery work. Historical failed approval commands remain
  in recent summaries instead of increasing open issue totals.
- Dashboard should show the same open Health issue summary as Health, not a
  separate count of historical warning rows.

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

P18 Health and Release Readiness:
  Health screen, visual QA, packaging/release workflow hardening
```

P16 should make the console useful for daily operator work. P17 should make
runtime policy editing product-grade. P18 should prepare the console for public
release artifacts.

## Verification Expectations

Every screen implementation phase should include:

- backend route tests for new API routes
- command route tests for mutations
- frontend lint, unit tests, typecheck, and build
- browser or screenshot verification for changed screens
- narrow-width verification
- reduced-motion sanity check when motion is added
- `git diff --check`
