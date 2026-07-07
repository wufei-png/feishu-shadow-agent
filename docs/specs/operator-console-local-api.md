# Operator Console Local API

Status: draft

This document defines the local HTTP API boundary for the Operator Console. It
is a product/API contract, not an implementation plan.

## Scope

This document answers:

- How the local console runtime serves the renderer and API.
- Which security constraints apply to the local API.
- Which routes the renderer can call.
- How routes map to `OperatorQueryService` and `OperatorCommandService`.
- How Settings, Product Policy, and Message Detail should be exposed.

This document does not answer:

- Exact Python files to change.
- Exact React component implementation.
- Exact release workflow YAML.
- Binary packaging details.
- Remote or multi-user access.

## Runtime Shape

The local console runtime is:

```text
Python local console server + bundled Vite/React renderer + local API
```

Default command shape:

```bash
python -m feishu_shadow_agent console --config config.yaml --host 127.0.0.1 --port 8765
```

Implementation default:

```text
FastAPI
```

Rationale:

- The console is product UI, not a temporary status page.
- The API needs structured request validation, structured errors, static asset
  serving, and testable route handlers.
- FastAPI is still small enough for a local-only Python runtime.

The renderer build is packaged with the local console runtime. Public
distribution is through GitHub Releases/tags, not GitHub Pages.

## Security Model

The local API is not a remote admin API.

Rules:

- Bind to `127.0.0.1` by default.
- Do not enable broad CORS.
- Validate `Host` against the selected local host and port.
- Generate a random bearer token on process start.
- Print an access URL that carries the token for the local browser session.
- The renderer stores the token for the session and removes it from the visible
  URL.
- All `/api/*` routes require the bearer token unless explicitly documented as a
  bootstrap route.
- Mutations set `actor` to `local_console`.
- Mutation request bodies may include `reason`.

Remote access, shared access, account auth, and multi-owner authorization are
out of scope.

## Response Conventions

Successful read responses return stable JSON DTOs directly.

Successful command responses return `CommandResult.as_dict()` shape from
`OperatorCommandService`.

Errors use a consistent envelope:

```json
{
  "error": {
    "code": "validation_failed",
    "message": "human-readable message",
    "details": {}
  }
}
```

Recommended HTTP status mapping:

```text
400 validation_failed
401 unauthorized
403 forbidden_origin_or_host
404 not_found
409 conflict
500 internal_error
503 store_unavailable
```

The renderer must not infer command success from HTTP 200 alone. It must use the
returned command `status`, `changed`, `warnings`, `result`, and `next_actions`.

## Read Routes

Read routes must map to `OperatorQueryService`. They must not call store helpers
directly from route handlers except through narrow query-service methods added
for the console contract.

```text
GET /api/dashboard
GET /api/approvals
GET /api/approvals/{approval_id}
GET /api/tasks
GET /api/tasks/{task_id}
GET /api/messages/{message_id}/detail
GET /api/dispatch/actions
GET /api/dispatch/actions/{action_id}
GET /api/policy/status
GET /api/policy/audits
GET /api/settings/catalog
GET /api/settings/runtime
GET /api/health/issues
```

List routes may accept:

```text
limit
offset
status
chat_id
scope
since
```

Exact filters are route-specific and should stay aligned with
`OperatorQueryService` capabilities.

## Health Issues

Route:

```text
GET /api/health/issues
```

Query-service method:

```text
OperatorQueryService.health_issues()
```

The response is a normalized diagnostic DTO, not a raw log or table dump:

```text
generated_at
summary.highest_severity
summary.open_issue_count
runtime.store
runtime.daemon_liveness
runtime.last_run
runtime.policy_status
issues[]
recent_failed_commands[]
recent_failed_dispatch_actions[]
```

Store status values:

```text
available
missing
unreadable
schema_uninitialized
schema_incompatible
```

Rules:

- The route handler must stay thin and call `OperatorQueryService`.
- Store and schema availability problems must be surfaced as normalized store
  issues instead of becoming a renderer-side empty state or API 500.
- `summary.open_issue_count` counts current actionable issues, not historical
  failures. Runtime health issues use the latest row per `check_name`; if that
  latest row is `ok`, older failures stay historical. Failed approval commands
  appear in `recent_failed_commands[]` but do not count as open issues unless a
  future command state gives them an explicit recovery path.
- `GET /api/dashboard` should reuse this summary for its Health issue metric
  instead of recounting historical `recent_health_warnings`.
- Dispatch actions in `failed` or `failed_needs_review` remain open issues until
  the operator retries, cancels, or marks them sent through Dispatch recovery.
- `runtime.last_run` and daemon liveness must use an allowlisted runtime summary;
  do not expose raw `runs` rows, `health_summary_json`, or
  `last_tick_summary_json`.
- Issue details and recent summaries must not expose raw log tails, arbitrary
  filesystem paths, full approval `/send` bodies, or direct SQL rows.
- Links should target existing console identifiers such as `task`, `message`,
  `approval`, `dispatch_action`, `policy`, and `settings`.

## Message Detail

`message_detail` is the canonical product name for viewing one Feishu message's
processing context.

Route:

```text
GET /api/messages/{message_id}/detail
```

Query-service method:

```text
OperatorQueryService.message_detail(message_id)
```

The DTO should include:

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

- It is read-only.
- It must not mutate approval expiry.
- It must not call `Dispatcher.preview_action()`, because dispatch preview
  records preview output.
- It may show existing action payload/result summaries already persisted in the
  store.
- If future UI needs to generate a fresh preview, that must be a separate
  command or temporary replay workflow, not part of `message_detail`.

## Command Routes

Command routes must map to `OperatorCommandService`.

Approval commands:

```text
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
POST /api/tasks/{task_id}/send
```

Task lifecycle commands:

```text
POST /api/tasks/{task_id}/close
POST /api/tasks/{task_id}/reopen
```

Dispatch recovery commands:

```text
POST /api/dispatch/actions/{action_id}/retry
POST /api/dispatch/actions/{action_id}/cancel
POST /api/dispatch/actions/{action_id}/mark-sent
```

Maintenance commands:

```text
POST /api/maintenance/expire-approvals
POST /api/maintenance/doctor
POST /api/maintenance/config-validate
POST /api/maintenance/retention-prune
POST /api/maintenance/reply-style-refresh
```

`expire-approvals` is shown from Dashboard/Approvals workflow context even
though the route namespace is maintenance. The other maintenance routes belong
to the Maintenance screen.

Policy commands:

```text
POST /api/policy/import-config
PATCH /api/policy/global
PATCH /api/policy/chats/{chat_id}
DELETE /api/policy/chats/{chat_id}
```

Policy preview routes are read-only deterministic impact previews, not command
mutations:

```text
POST /api/policy/global/preview
POST /api/policy/chats/{chat_id}/preview
POST /api/policy/chats/{chat_id}/delete-preview
```

All command responses use the command result shape. Preview routes return
structured field changes, effective before/after policy, behavior changes, and
affected summaries; they must not write audits, assign risk levels, or create
confirmation-required workflows. The renderer shows command feedback from the
result, then invalidates and refreshes affected query groups.

## Settings And Policy Mutation

Product Policy and chat policy are runtime product settings and may be edited
through policy command routes.

The console must not write `config.yaml` in v1. Non-policy configuration can be
shown as settings metadata, readonly runtime state, diagnostics, or advanced
fields, but writing YAML config needs a future `ConfigCommandService` or
equivalent command facade.

The Settings Catalog is a stable product field map for console-exposed fields.
It is not a dynamic form engine and does not mirror every raw config field.

Settings sources:

```text
product_policy_store
config_yaml
runtime_status
derived
```

Settings visibility:

```text
normal
advanced
readonly
diagnostic
hidden
```

Risk levels and UI-owned risk taxonomy are not part of the API. Policy mutation
commands apply valid changes directly and write audit records.

## Refresh Ownership

The renderer is never the source of truth.

Rules:

- Dashboard and queue views may poll.
- Detail views refresh after relevant mutations.
- Mutations invalidate affected TanStack Query groups.
- Background refresh must not overwrite an in-progress Settings or Policy edit.
- Optimistic UI is only allowed for harmless local state such as selection,
  expansion, and panel visibility.

## Non-Goals

- No GitHub Pages runtime.
- No remote API.
- No account login.
- No multi-owner permission model.
- No direct SQLite access from the renderer.
- No renderer calls to store helpers.
- No `config.yaml` write path in v1.
- No risk confirmation workflow.
