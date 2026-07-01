# P17 Operator Console Policy And Settings Plan

## Summary

P17 implements the final Product Policy and Settings experience for the local
Operator Console. It turns the existing policy command/query backend and
Settings Catalog into product-grade UI screens.

This phase owns:

```text
Policy
Settings
Policy Import Diff
Policy Audit History
```

P17 follows P16. It should not revisit the core operator queue screens except to
link to Policy or Settings where needed.

## Background

P10-P14 established Product Policy Store as runtime truth and
`OperatorCommandService` as the mutation facade. P15 created the local console
runtime and settings catalog routes. P16 implements the core daily operator
workflows.

The final screen contract is defined by:

- `docs/specs/operator-console-screen-flows.md`
- `docs/specs/operator-console-ui-system.md`
- `docs/specs/operator-console-local-api.md`
- `docs/specs/operator-console-settings-catalog.md`

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/plans/p16-operator-console-core-screens.md`
- `docs/specs/operator-console-screen-flows.md`
- `docs/specs/operator-console-settings-catalog.md`
- `docs/specs/operator-console-local-api.md`
- `src/feishu_shadow_agent/console_api.py`
- `src/feishu_shadow_agent/operator_query.py`
- `src/feishu_shadow_agent/operator_commands.py`
- `src/feishu_shadow_agent/settings_catalog.py`
- `frontend/operator-console/src/`
- `tests/test_console_api.py`
- `tests/test_operator_commands.py`

## Dependencies

- P16 must be complete or at least must not change the route/security patterns
  established by P15.
- Product Policy Store must remain runtime truth.
- `config.yaml.reply_policy` and `config.yaml.chats` remain Policy Import Source,
  not runtime truth.
- `config.yaml` writeback remains out of scope.

## Goals

### Backend API

Add policy read routes:

```text
GET /api/policy/status
GET /api/policy/audits
```

Add policy command routes:

```text
POST /api/policy/import-config
PATCH /api/policy/global
PATCH /api/policy/chats/{chat_id}
```

Keep Settings routes:

```text
GET /api/settings/catalog
GET /api/settings/runtime
```

Policy editor read model:

```text
GET /api/settings/runtime
  global_policy
  chat_policies
  values
  policy_status
  policy_audit_history
```

P17 should use `/api/settings/runtime` as the source for current global policy,
chat policy rows, catalog-backed values, and readonly runtime settings. Do not
invent new dedicated policy value routes unless `docs/specs/operator-console-local-api.md`
is updated in the same change and route tests are added.

All policy command routes must use `OperatorCommandService` with
`actor="local_console"` and return `CommandResult.as_dict()`.

### Policy Screen

Implement a product-grade Policy screen:

- Product Policy initialization state.
- Policy Import Diff summary and detail.
- Global policy editor.
- Chat policy list and editor.
- Policy audit history.
- Command result feedback after import/update commands.

Policy fields must use product labels and field help from Settings Catalog where
appropriate.

### Settings Screen

Implement the Settings screen as a product field map, not a raw config editor:

- Normal settings.
- Advanced settings.
- Diagnostics.
- Hidden fields omitted.

In v1, `config_yaml` fields are readonly. The UI may show current values and
restart requirements, but it must not write `config.yaml`.

### Interaction

- Preserve in-progress policy edits during background refresh.
- Show diffs before saving policy changes.
- Apply valid policy changes directly; do not use risk levels or confirmation
  required flows.
- Render command result feedback from the backend response.
- Refresh policy status, audit history, dashboard, and settings runtime after
  successful policy mutations.

## Non-goals

- No `config.yaml` write path.
- No `ConfigCommandService`.
- No risk levels or risk confirmation workflow.
- No remote access.
- No account/login model.
- No full Logs / Health screen.
- No binary packaging or release workflow changes.
- No direct SQLite reads from route handlers or renderer code.

## Recommended File Scope

Backend:

```text
src/feishu_shadow_agent/console_api.py
src/feishu_shadow_agent/operator_query.py
src/feishu_shadow_agent/operator_commands.py
tests/test_console_api.py
tests/test_operator_query.py
tests/test_operator_commands.py
```

Frontend:

```text
frontend/operator-console/src/
```

Expected frontend modules if P16 split the shell:

```text
frontend/operator-console/src/api.ts
frontend/operator-console/src/types.ts
frontend/operator-console/src/components/
frontend/operator-console/src/screens/PolicyScreen.tsx
frontend/operator-console/src/screens/SettingsScreen.tsx
```

Docs:

```text
docs/specs/operator-console-settings-catalog.md
docs/specs/operator-console-screen-flows.md
docs/testing.md
```

Only update docs when implementation changes the screen contract.

## Implementation Notes

### Policy Import Diff

Policy Import Diff explains what `policy import-config` or replace/import would
change relative to Product Policy Store. It is not runtime drift.

The UI should not use "config drift" language.

### Policy Editors

Global policy editor fields:

```text
p2p_auto_reply
unknown_group_auto_reply
default bot_joined
default reply_identity
default allow_user_fallback
default resource_download
```

Chat policy editor fields:

```text
name
auto_reply
bot_joined
reply_identity
allow_user_fallback
resource_download
```

Field help explains meaning. It does not recommend choices.

### Audit History

Audit history should prioritize:

```text
created_at
actor
reason
scope
policy_key
old_summary
new_summary
```

Long raw JSON should be hidden behind detail expansion if exposed at all.

### Settings

Settings should group fields by visibility and product meaning. Do not render
the catalog as a flat table in the final UI.

Readonly config fields should show:

```text
current value
source
requires restart
why readonly in v1
```

Hidden fields should not appear in the default UI.

### Query Invalidation

After policy import/update commands, invalidate:

```text
dashboard
policy-status
policy-audits
settings-runtime
settings-catalog
tasks
message-detail
```

Policy changes can alter effective policy shown in task/message detail.

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

- Policy screen shows initialized/uninitialized states.
- Policy Import Diff renders matches/differs/unknown states.
- Policy editor loads current global policy and chat policies from
  `/api/settings/runtime`.
- Global policy edits preserve input on failed mutation.
- Chat policy edits update through command result.
- Policy import/update refreshes policy status, settings runtime, dashboard, and
  audit history.
- Validation or conflict command results do not clear unsaved policy edits.
- Audit history refreshes after policy mutation.
- Settings screen shows Normal, Advanced, and Diagnostics sections.
- Narrow-width layout remains usable.

## Acceptance

- Policy screen can import config policy through the command facade.
- Policy screen can update global policy and chat policy.
- Policy screen reads current global and chat policy state from the declared
  `/api/settings/runtime` read model.
- Policy updates apply directly with audit records and without risk confirmation.
- Policy Import Diff is rendered without "config drift" terminology.
- Policy audit history is available and filterable enough for v1.
- Settings screen renders console-exposed settings grouped by visibility.
- `config_yaml` fields are readonly in v1.
- Background refresh does not overwrite in-progress Policy or Settings edits.
- All new API routes require bearer token and local Host validation.
- Route handlers use QueryService/CommandService boundaries.
- Frontend build passes.
- Relevant backend tests pass.
- `git diff --check` passes.

## Handoff Notes

- P17 makes policy editing product-grade. It should not become a raw config
  editor.
- Keep Product Policy Store as runtime truth.
- Keep Settings Catalog as stable product metadata, not a dynamic schema engine.
- Do not write `config.yaml`.
- Do not add risk levels or confirmation-required behavior.
- Logs / Health and release hardening remain P18.
