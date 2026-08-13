# P15 Operator Console Foundation Plan

## Summary

P15 starts the local Operator Console implementation. It adds the local console
runtime, FastAPI API shell, Vite/React/TypeScript renderer scaffold, static asset
packaging, and the first read-only console contracts needed for later screens.

This phase is a foundation phase, not the full console UI. It should make the
console run locally, prove the API/security/static-serving boundary, and leave
screen-by-screen product work for follow-up phases.

## Background

P10-P14 fixed the operator-surface backend boundary:

- `OperatorQueryService` owns read-only operator DTOs.
- `OperatorCommandService` owns explicit mutations.
- Product Policy Store is the runtime source of truth.
- Query paths must not mutate approval expiry, dispatch recovery, or policy.

The UI direction is now defined by:

- `docs/specs/operator-console-ui-system.md`
- `docs/specs/operator-console-local-api.md`
- `docs/specs/operator-console-settings-catalog.md`

P15 should implement the minimum local-console foundation that respects those
documents.

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/plans/operator-surface-outline.md`
- `docs/specs/operator-console-ui-system.md`
- `docs/specs/operator-console-local-api.md`
- `docs/specs/operator-console-settings-catalog.md`
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/operator_query.py`
- `src/feishu_shadow_agent/operator_commands.py`
- `src/feishu_shadow_agent/config.py`
- `src/feishu_shadow_agent/dispatcher.py`
- `src/feishu_shadow_agent/store/sqlite_store.py`

## Dependencies

- P13 must be complete so the console reads through `OperatorQueryService`.
- P14a/P14b must be complete so console mutations go through
  `OperatorCommandService`.
- The local API and settings specs must exist before implementing this phase.

## Goals

- Add a local `console` CLI command:

  ```bash
  python -m feishu_shadow_agent console --config config.yaml --host 127.0.0.1 --port 8765
  ```

- Add a FastAPI app factory for the local console.
- Bind to `127.0.0.1` by default.
- Generate and enforce a per-process bearer token for `/api/*`.
- Validate local `Host` headers.
- Serve the bundled renderer build from the Python package.
- Add a Vite + React + TypeScript renderer scaffold.
- Add semantic CSS token foundation and the product shell skeleton:
  `AppShell`, `RuntimeStrip`, left navigation, and a dashboard placeholder.
- Add `GET /api/dashboard`.
- Add `GET /api/settings/catalog`.
- Add `GET /api/settings/runtime`.
- Add `OperatorQueryService.message_detail(message_id)` and
  `GET /api/messages/{message_id}/detail`.
- Ensure `message_detail` is read-only and does not call
  `Dispatcher.preview_action()`.
- Keep Product Policy mutation routes out of P15 unless required by the shell.
- Add tests for auth, Host validation, static serving, dashboard route, settings
  catalog route, and message detail read-only behavior.
- Add packaging metadata so built renderer assets are included in Python package
  artifacts.

## Non-goals

- No full screen implementation for Approvals, Tasks, Dispatch, Policy, Settings,
  or Logs / Health.
- No remote access.
- No login/account model.
- No GitHub Pages runtime.
- No Electron or Tauri wrapper.
- No binary packaging.
- No `config.yaml` write path.
- No risk confirmation workflow.
- No direct SQLite reads from the renderer.
- No renderer calls to store helpers.
- No dispatch preview generation from `message_detail`.

## Recommended File Scope

Python runtime:

```text
src/feishu_shadow_agent/cli.py
src/feishu_shadow_agent/console_api.py
src/feishu_shadow_agent/console_security.py
src/feishu_shadow_agent/settings_catalog.py
src/feishu_shadow_agent/operator_query.py
src/feishu_shadow_agent/console_static/
pyproject.toml
```

Renderer:

```text
frontend/operator-console/package.json
frontend/operator-console/tsconfig.json
frontend/operator-console/vite.config.ts
frontend/operator-console/index.html
frontend/operator-console/src/
```

Tests:

```text
tests/test_console_api.py
tests/test_operator_query.py
```

Docs:

```text
docs/testing.md
docs/configuration.md
docs/specs/operator-console-local-api.md
docs/specs/operator-console-settings-catalog.md
```

Only update additional docs when the implementation changes the operator-facing
workflow.

## Implementation Notes

### FastAPI App

Create an app factory that accepts loaded config or config path dependencies
rather than relying on global process state. The route layer should be thin:

```text
HTTP request -> validation/security -> OperatorQueryService/OperatorCommandService -> JSON response
```

Do not put query SQL or store helper orchestration directly in route handlers.

### Static Renderer

Keep source code under `frontend/operator-console/`. Build output should be
copied or emitted into a package-data directory under
`src/feishu_shadow_agent/console_static/`.

The Python package should include built assets through `pyproject.toml`
package-data configuration.

If frontend build assets are absent in a source checkout, the console command
should fail with a clear message that explains how to build the renderer.

### Security

Generate a token on startup. Print a local URL that includes the token, for
example:

```text
http://127.0.0.1:8765/#token=<token>
```

The renderer should store the token for the current browser session and remove
it from the visible URL.

All `/api/*` routes should require:

```text
Authorization: Bearer <token>
```

Do not enable broad CORS. Do not make remote bind the default.

### Settings Catalog

`GET /api/settings/catalog` should return static metadata from
`settings_catalog.py`.

`GET /api/settings/runtime` should return current runtime/config values for
catalog-exposed fields. In P15, `config_yaml` fields are readonly.

Do not create a dynamic schema engine. The catalog is a stable product field
map.

### Message Detail

Add `OperatorQueryService.message_detail(message_id)` as the formal read model
for a message's processing context.

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

The implementation may reuse safe read SQL currently represented by
`SQLiteStore.replay_summary()`, but it must live behind `OperatorQueryService`
and must open the store in read-only mode like the rest of the query service.

It must not call `Dispatcher.preview_action()` or record new previews.

### Renderer Shell

Implement only the shell and a minimal live dashboard placeholder in P15:

- left navigation
- top runtime strip
- main content area
- dashboard route that reads `/api/dashboard`
- settings route placeholder that reads `/api/settings/catalog`
- message detail route placeholder may be developer-facing only if no full Tasks
  screen exists yet

Use semantic CSS tokens and dark-first styling. Avoid a generic admin template.

## Test Plan

Python tests:

```bash
.venv/bin/python -m pytest -q tests/test_console_api.py tests/test_operator_query.py
```

Full backend tests:

```bash
.venv/bin/python -m pytest -q
```

Frontend validation should be exposed through package scripts, for example:

```bash
npm --prefix frontend/operator-console install
npm --prefix frontend/operator-console run typecheck
npm --prefix frontend/operator-console run build
```

Whitespace check:

```bash
git diff --check
```

If browser verification is practical in the implementation environment, capture
at least one desktop screenshot and one narrow-width screenshot of the shell.

## Acceptance

- `python -m feishu_shadow_agent console --help` shows the console command.
- The console server binds to `127.0.0.1` by default.
- The startup output includes a local URL with a token.
- `/api/dashboard` rejects missing/invalid token.
- `/api/dashboard` returns an `OperatorQueryService.dashboard_snapshot()`
  compatible DTO with a valid token.
- `/api/settings/catalog` returns the static console field map.
- `/api/settings/runtime` returns readonly runtime/config values for exposed
  settings.
- `/api/messages/{message_id}/detail` returns a read-only message detail DTO.
- Message detail does not mutate approvals, actions, dispatch attempts, or action
  preview results.
- Built renderer assets are served by the console runtime.
- Python package metadata includes the built renderer assets.
- Renderer build/typecheck pass.
- Backend tests pass.
- `git diff --check` passes.

## Handoff Notes

- P15 opens the actual local console implementation; it is no longer part of the
  P10-P14 "no Web UI" backend-contract round.
- Keep route handlers thin and service-backed.
- Keep `message_detail` read-only. Fresh preview generation belongs to a future
  command or replay workflow, not this query.
- Do not write `config.yaml` in P15.
- Do not reintroduce risk levels or confirmation-required policy updates.
- Do not add Electron/Tauri or binary packaging yet.
