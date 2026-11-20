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
- Inspect daemon liveness and open Health issues.
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
diff expansion; respect reduced motion. The library owns mount/unmount and layout
(`AnimatePresence`, `layoutId`). CSS handles hover, press, colour, and focus.
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

- Support light and dark as peers, following the system appearance by default.
- Settings stores the color scheme and material independently. Clear Glass is
  the default material. Graphite uses opaque surfaces and a neutral palette.
- Design tokens must be semantic. Do not hard-code theme-only token names such as
  `--dark-bg`.
- Use a quiet cool tint for Clear Glass and neutral greys for Graphite. Keep
  semantic status colors consistent across materials.
- Use status colors only for state, command feedback, and attention.
- Avoid decorative gradients, glowing backgrounds, large hero surfaces, and
  ornamental animation.
- Give the clear floating surfaces a curved rim with directional reflections.
  Chromium also displaces the backdrop along the bevel. Keep content panels and
  form controls opaque.
- Limit glass to the desktop runtime capsule or compact navigation bar.
  Use opaque surfaces with reduced transparency, increased contrast, forced colors, or absent
  `backdrop-filter` support.
- Use small, tool-like radius values, and keep nested radii concentric: an inner
  radius is the outer radius minus the gap between them.
- Use tabular numbers for counters, timings, and metrics.
- Make hierarchy with weight, contrast, and spacing before adding color.
- One `h1` per view. Empty, loading, and error states are `h2`; readouts nested
  inside a panel are `h3`.

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
--chrome-fill
--chrome-sheen
--chrome-filter
--elevation-1
--elevation-2
```

## Information Architecture

The console shell should use:

- Left navigation.
- Top runtime strip.
- Main work area.
- Persistent detail panel or drawer when detail context benefits from staying in
  the current workflow.

Primary navigation is grouped into three sections rather than presented as one
flat list, because the grouping states what each destination is for:

```text
Duty        Dashboard, AI Coworker, Approvals, Tasks, Dispatch, Feedback
Configure   Policy, Settings
System      Health, Maintenance
```

At 900px and below, a pinned top bar shows the current destination. It opens a
menu with the same three groups. The runtime summary scrolls with the content.

Navigation intent:

- `Dashboard` is the default entry and must be actionable.
- `Approvals` is the primary work queue.
- `Tasks` tracks conversation/task context.
- `Dispatch` handles send recovery and readback evidence.
- `Policy` manages Product Policy and chat policy.
- `Settings` manages local console and runtime configuration.
- `Health` is diagnostic and should not clutter the dashboard.

Do not create a landing or welcome page as the primary experience.

## Dashboard Focus

The dashboard first viewport should focus on what needs the operator's
attention, not decorative metrics.

Priority order:

```text
1. Dispatch actions to verify or recover.
2. Pending and overdue approvals.
3. Blocked or failed tasks.
4. Product Policy, runtime health, and recent audit highlights.
```

Avoid making these the primary focus:

- Total message counts.
- Total task counts.
- Cumulative reply counts.
- Resource download totals.
- Attractive but non-actionable trend charts.

Suggested composition:

- Runtime strip with daemon, policy, import difference, and data freshness.
- Compact action index followed by dispatch and approval previews.
- Next-step guidance when runtime state requires attention.
- Secondary context: Health issue summary, recent audit, policy diff.
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
- Health: normalized issue list plus diagnostic detail; it is not a raw
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

Boundary between the library and CSS:

- The animation library owns mount, unmount, and layout: route content, detail
  panels keyed on the selection, list rows, command results, and shared-element
  selection indicators.
- CSS owns hover, press, colour, and focus transitions.
- Route changes cross-fade with no travel and no exit animation. A sidebar is a
  high-frequency control and macOS does not translate between sidebar destinations
  either; holding the outgoing screen for even 100ms reads as the app being slow,
  and a true cross-fade would require overlapping both screens in the layout.
- `reducedMotion="user"` is authoritative: under `prefers-reduced-motion` the
  library drops transforms and keeps opacity, so the state change is still
  reported without movement.

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
EmptyQueueState
HealthIssueList
AuditTrail
```

Shared primitives may exist, but they support product components:

```text
Button
ConfirmButton
IconButton
TextField
Textarea
Switch
Select
SegmentedControl
Badge
Tooltip
Panel
```

`ConfirmButton` is the required pattern for destructive commands: the first click
arms the action and states exactly what will happen, the second commits, and an
armed button disarms itself after a few seconds so a half-pressed destructive
command cannot sit waiting on screen. Do not use `window.confirm`, which blocks
the page, cannot be styled or tested, and reports nothing when the operator
cancels.

Icon usage:

- Use `lucide-react`.
- Use icons for navigation, command buttons, statuses, and field help.
- Keep icons selective and functional.
- Icon-only controls must have accessible labels and tooltips.

## Settings And Policy Fields

The Settings screen has two layers, in this order:

1. **Local preferences.** The operator's own appearance and language, stored on
   this machine and applied immediately. These are the only editable controls on
   the page. They sit above the catalog rather than inside it: a preference is not
   a product field, it is never searched or filtered, and it must stay reachable
   while the catalog is filtered down to nothing.
2. **Product field catalog.** A read-only index of console-exposed fields, grouped
   and searchable. Editing happens where the field's write boundary allows it:
   Product Policy on the Policy page, `config.yaml` through its own command and
   audit path.


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
