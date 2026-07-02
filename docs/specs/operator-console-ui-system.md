# Operator Console UI System

Status: draft

This document defines the UI system and renderer conventions for the local
Operator Console. It is intentionally limited to product UI direction,
interaction patterns, component strategy, and renderer conventions.

It is not an implementation plan. It does not define API routes, file changes,
release steps, Python server details, or screen-by-screen delivery order.

## Scope

This document answers:

- What the Operator Console should feel like.
- Which renderer stack and UI libraries are the default path.
- How visual tokens, icons, motion, states, and layout should be used.
- Which product components should shape the UI.
- How Settings and Policy fields should be explained.
- Which UI/backend boundaries must not be crossed.

This document does not answer:

- Which screen ships first.
- Which local API endpoints exist.
- How the Python local console server is implemented.
- How the Vite app is scaffolded.
- Which files are updated in each implementation phase.
- The full console-exposed Settings Catalog field table.

The full console-exposed settings field table is intentionally out of scope for
this document. It must be defined separately before Settings and Policy editing
is implemented. Recommended target:

```text
docs/specs/operator-console-settings-catalog.md
```

This document is self-contained. Do not inspect external projects to interpret
or implement it.

## Product Intent

The Operator Console is a local operations workbench for the single
owner/operator running Feishu Shadow Agent.

The operator opens it to:

- Review pending or overdue approvals.
- Understand why a task is blocked.
- Recover failed or stale dispatch actions.
- Inspect daemon liveness and health warnings.
- Manage Product Policy and chat policy.
- Review recent command results and policy audit history.

The console is not:

- A marketing page.
- A generic admin dashboard.
- A chat client.
- A raw log viewer.
- A database browser.

The UI should feel quiet, precise, trustworthy, and high-signal. It should be
dense enough for repeated operator use while keeping hierarchy clear.

## Renderer Stack

The renderer stack is fixed:

```text
Renderer: Vite + React + TypeScript
```

Distribution target:

- No GitHub Pages runtime target.
- Public distribution is through GitHub Releases/tags.
- The renderer build is packaged with the local console runtime.
- The runtime shell remains Python local console first.
- Electron or Tauri may be considered later as packaging wrappers, but they are
  not the current UI architecture.

Default UI library strategy:

```text
Icons: lucide-react.
Animation: motion, limited to state transitions, overlays, list entry/exit, and
diff expansion; respect reduced motion.
Server state: TanStack Query for all local API queries, mutations, polling, and
invalidation.
Headless primitives: Radix UI Primitives when native HTML is insufficient; do
not use Radix Themes as the visual system.
Forms: native/controlled React for tiny one-off forms; TanStack Form for
Settings and Policy forms once Settings Catalog backed editing begins.
Tables: TanStack Table for sortable/filterable operator lists when list behavior
becomes non-trivial.
Virtualization: TanStack Virtual only for large lists or long audit/history
views.
Styling: semantic CSS tokens plus product components; do not adopt a generic
admin UI kit as the design source.
```

Use native HTML controls first. Use Radix UI Primitives for dialogs, popovers,
tooltips, tabs, dropdowns, selects, and focus-managed overlays only when native
HTML is insufficient. Do not use a general-purpose admin template as the visual
source.

## Visual Language

The Operator Console should read as a local operations workbench:

- Quiet, precise, and trustworthy.
- High-signal, not decorative.
- Medium-high density for repeated use.
- Designed around queues, blockers, recovery, policy, and audit.
- Not playful, not marketing-oriented, and not generic admin.

Do not write a complete token value table in this document. Token values should
land with the frontend shell implementation. This document defines direction and
constraints.

Visual rules:

- Use a dark-first theme for v1.
- Design tokens must be semantic and future-compatible with light mode.
- Do not hard-code dark-only token names such as `--dark-bg`.
- Use low-saturation neutral surfaces.
- Use status colors only for state, command feedback, and attention.
- Avoid decorative gradients, glowing backgrounds, large hero surfaces, and
  ornamental animation.
- Prefer subtle borders and surface elevation over heavy shadows.
- Use small, tool-like radius values.
- Use tabular numbers for counters, timings, and metrics.
- Make hierarchy with weight, contrast, and spacing before adding color.

Recommended semantic token families:

```text
--surface-canvas
--surface-panel
--surface-raised
--surface-inset
--text-primary
--text-secondary
--text-muted
--border-subtle
--border-strong
--accent-primary
--status-success
--status-warning
--status-danger
--status-info
```

## Information Architecture

The console shell should use:

- Left navigation.
- Top runtime strip.
- Main work area.
- Persistent detail panel or drawer when detail context benefits from staying in
  the current workflow.

Primary navigation:

```text
Dashboard
Approvals
Tasks
Dispatch
Policy
Settings
Logs / Health
```

Navigation intent:

- `Dashboard` is the default entry and must be actionable.
- `Approvals` is the primary work queue.
- `Tasks` tracks conversation/task context.
- `Dispatch` handles send recovery and readback evidence.
- `Policy` manages Product Policy and chat policy.
- `Settings` manages local console and runtime configuration.
- `Logs / Health` is diagnostic and should not clutter the dashboard.

Do not create a landing or welcome page as the primary experience.

## Dashboard Focus

The dashboard first viewport should focus on what needs the operator's
attention, not decorative metrics.

Priority order:

```text
1. Pending approvals and overdue approvals.
2. Failed or stale dispatch actions.
3. Product Policy initialization state and Policy Import Diff.
4. Daemon liveness and health warnings.
5. Recent command results and audit highlights.
```

Avoid making these the primary focus:

- Total message counts.
- Total task counts.
- Cumulative reply counts.
- Resource download totals.
- Attractive but non-actionable trend charts.

Suggested composition:

- Top runtime strip: daemon, policy, last tick, local API status.
- Central action queue: approvals and dispatch recovery.
- Secondary context: health warnings, recent audit, policy diff.
- Empty state: clearly state that there are no pending operator actions while
  still showing daemon/policy status.

## Interaction Model

The default interaction model is master-detail:

```text
List or queue -> persistent detail panel -> command/result feedback
```

Use this pattern for:

- Approvals: queue plus approval/task/context detail.
- Tasks: task list plus task timeline/detail.
- Dispatch: action list plus attempts/readback/detail.
- Policy: global/chat policy list plus editor/diff/audit.
- Logs / Health: normalized issue list plus diagnostic detail; it is not a raw
  log viewer.

Avoid full-page navigation for every selected object. Operators need to keep
queue context while processing multiple items.

Avoid modals in v1. Use persistent detail panels, drawers, inline editors,
popovers, and toasts. Introduce modal/dialog only when a future workflow cannot
be handled without an interruptive focus trap; if introduced, use Radix Dialog
rather than hand-rolled focus management.

## Motion

Motion exists to clarify state changes, not to decorate.

Allowed motion:

- List item entry and removal.
- Detail panel transitions.
- Drawer or panel expansion.
- Policy diff expansion and collapse.
- Toast entry and exit.
- Very subtle skeleton loading, or static skeletons.
- Button active press feedback.

Avoid:

- Large page-level entrance animations.
- Background particles, glow, or ambient animation.
- Dashboard card loops.
- Decorative number scrolling.
- Long animation on high-frequency actions.
- `transition: all`.

Motion parameters:

```text
Common duration: 120-220ms
Maximum duration: 300ms
Easing: ease-out or cubic-bezier(0.23, 1, 0.32, 1)
Animated properties: transform, opacity, and color
Reduced motion: always respect prefers-reduced-motion
```

## Product Components

Build around product semantics, not a generic UI kit.

Product components:

```text
AppShell
RuntimeStrip
ActionQueue
ApprovalRow
ApprovalDetailPanel
TaskTimeline
DispatchActionRow
DispatchReadbackPanel
PolicyScopeList
PolicyEditorPanel
PolicyDiffPreview
MessageDetailPanel
SettingsCatalogSection
SettingsField
FieldHelpTooltip
CommandResultToast
EmptyQueueState
HealthIssueList
AuditTrail
```

Shared primitives may exist, but they support product components:

```text
Button
IconButton
TextField
Textarea
Switch
Select
SegmentedControl
Badge
Toast
Tooltip
Panel
```

Icon usage:

- Use `lucide-react`.
- Use icons for navigation, command buttons, statuses, and field help.
- Keep icons selective and functional.
- Icon-only controls must have accessible labels and tooltips.

## Settings And Policy Fields

Every product-relevant configurable behavior should eventually have an
operator-facing UI. Not every low-level config field should be editable in the
normal UI.

Configuration should be represented through a Settings Catalog, not by
auto-rendering the raw Pydantic schema. The Settings Catalog is a stable product
field map for console-exposed settings; it is not a dynamic schema engine and
does not need to mirror every raw config field.

Each Settings Catalog entry should define:

```text
key
label
description
help
source
scope
visibility
editable
requires_restart
audit_behavior
```

Field explanation pattern:

- Use a short product label.
- Include a one-sentence inline description.
- Add a question-mark help tooltip only for concepts that are easy to
  misunderstand.
- Tooltips explain what the setting does; they do not tell the operator what
  decision to make.
- Do not use risk levels or UI-owned risk taxonomy.
- Do not put critical information only inside a tooltip.

Fields that likely need help tooltips include:

```text
allow_user_fallback
reply_identity
bot_joined
resource_download
unknown_group_auto_reply
approval_timeout_hours
```

Example:

```text
Label: Allow user fallback
Description: Let group replies fall back to your user identity when
bot-preferred replies cannot use the bot.
Help: Applies only when reply identity is bot preferred. If the bot is
unavailable in a group, the assistant may reply as you instead.
```

## UI And Backend Boundary

The renderer must stay behind the local console API.

Rules:

- The renderer must not read SQLite directly.
- The renderer must not call store helpers directly.
- The renderer must not infer command success from optimistic UI state.
- The renderer must consume operator read models and command results from the
  local console API.
- The local console API maps requests to `OperatorQueryService` and
  `OperatorCommandService`.
- UI-facing policy and settings mutations must not bypass the command facade.

Do not define concrete API routes in this document. Route contracts belong in a
local console API plan/spec.

## Data Refresh

The renderer is never the source of truth.

Refresh principles:

- Dashboard and queue views may poll.
- Detail views should refresh after command mutations.
- Mutations must invalidate affected TanStack Query groups.
- Use command results for feedback, then refresh authoritative read models.
- Do not pretend approve/send/retry commands succeeded before the backend
  confirms them.
- Preserve user input when a mutation fails.
- Do not let background refresh overwrite an in-progress Settings or Policy
  edit.
- Optimistic UI is acceptable only for harmless local state such as row
  selection, expansion, and panel visibility.

Do not define polling intervals in this document. Intervals belong in page or
API implementation plans.

## Accessibility And Keyboard

Baseline requirements:

- All interactive controls must be keyboard reachable.
- Focus states must be visible.
- Icon-only buttons require accessible labels and tooltips.
- Tooltips must not be the only location for critical information.
- Form errors must be attached to the relevant fields.
- Reduced motion must be respected.
- Color must not be the only status indicator.
- Primary actions must not depend on hidden keyboard shortcuts.

Keyboard shortcuts and command palette behavior may be added later, but they are
not required for v1.

## Verification Baseline

Any implementation that changes renderer UI must include:

- Typecheck and build validation.
- Relevant tests where UI logic exists.
- Screenshot or browser verification for major screens.
- Reduced-motion sanity checks when motion is added.
- Narrow-width sanity checks for new layout work.

Concrete commands belong in the relevant implementation plan or package scripts.
This document sets the quality bar, not the command list.
