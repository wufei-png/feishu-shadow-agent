# P18 Operator Console Health And Release Readiness Plan

## Summary

P18 completes the local Operator Console for release readiness after P16 and
P17. It owns:

```text
Health
cross-screen visual QA
static asset and Python package release validation
GitHub tag/release artifact notes
```

This phase should make the console shippable as a local product surface. It
should not turn the project into a remote web app, a log viewer, or a desktop
binary project.

## Background

P15 created the local console runtime, FastAPI shell, renderer scaffold, token
security, Host validation, and static asset packaging. P16 implements the daily
operator screens. P17 implements Product Policy and Settings.

P18 is the final UI-readiness pass. It should verify the whole console behaves
as one coherent product and that GitHub release artifacts can contain the built
renderer.

The final screen contract is defined by:

- `docs/specs/operator-console-screen-flows.md`
- `docs/specs/operator-console-ui-system.md`
- `docs/specs/operator-console-local-api.md`
- `docs/specs/operator-console-settings-catalog.md`

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/plans/p16-operator-console-core-screens.md`
- `docs/plans/p17-operator-console-policy-settings.md`
- `docs/specs/operator-console-screen-flows.md`
- `docs/specs/operator-console-ui-system.md`
- `docs/specs/operator-console-local-api.md`
- `src/feishu_shadow_agent/console_api.py`
- `src/feishu_shadow_agent/operator_query.py`
- `src/feishu_shadow_agent/operator_commands.py`
- `frontend/operator-console/src/`
- `frontend/operator-console/package.json`
- `pyproject.toml`
- `docs/testing.md`

## Dependencies

- P16 and P17 must be complete.
- Renderer assets must continue to be served by the Python local console server.
- Public distribution remains GitHub tags/releases, not GitHub Pages.
- Binary packaging remains out of scope unless explicitly reopened.

## Goals

### Backend API

Add a health read route:

```text
GET /api/health/issues
```

The route should return a normalized health DTO derived through
`OperatorQueryService`, not direct renderer SQLite reads.

P18 should add a query-owned method such as:

```text
OperatorQueryService.health_issues()
```

Do not derive health only by combining `dashboard_snapshot()` fields. Some
existing read models intentionally degrade unavailable store reads into empty
lists or uninitialized status. Health must surface store and schema
availability problems explicitly.

Suggested DTO shape:

```json
{
  "generated_at": "2026-07-01T00:00:00Z",
  "summary": {
    "highest_severity": "warning",
    "open_issue_count": 2
  },
  "runtime": {
    "daemon_liveness": {},
    "last_run": {}
  },
  "issues": [
    {
      "id": "dispatch-action-42",
      "severity": "error",
      "category": "dispatch",
      "title": "Dispatch action needs review",
      "detail": "Action 42 failed after the actual-send boundary.",
      "detected_at": "2026-07-01T00:00:00Z",
      "links": [
        {"type": "dispatch_action", "id": "42"}
      ],
      "recommended_actions": [
        "inspect",
        "mark_sent",
        "retry",
        "cancel"
      ]
    }
  ]
}
```

The DTO should derive issues from existing operator read models where possible:

- daemon liveness and last run summary
- Product Policy initialization and invalid policy state
- recent health warnings
- recent failed approval command summaries, not open issues
- failed or `failed_needs_review` dispatch actions
- stale sending actions
- store unavailable or read errors

Do not expose arbitrary filesystem paths, raw log tails, or direct SQL rows.

Store availability requirements:

- Probe whether the SQLite store can be opened and core tables are present.
- Return a normalized `store` or `runtime` health issue when the store is missing,
  unreadable, unmigrated, or schema-incompatible.
- Do not include absolute database, log, or resource paths in default issue
  details.
- Add tests for missing/unmigrated/unreadable store behavior where practical.

### Health Screen

Implement the final Health screen:

- Current health issue list.
- Health issue detail panel.
- Recent failed command summaries.
- Recent failed dispatch action summaries.
- Runtime liveness details.
- Links to relevant task, message, approval, or dispatch action detail.

The screen should answer:

```text
What is broken?
How severe is it?
What object should I inspect?
What operator action is available?
```

Raw logs are not part of the default screen. If a focused log excerpt is added,
it must be limited to known project log files, size-bounded, redacted, and
optional behind a detail affordance.

### Cross-screen Polish

Run a final UI pass across all console screens:

- Dashboard
- Approvals
- Tasks
- Message Detail
- Dispatch
- Policy
- Settings
- Health

Required states:

- loading
- empty
- error
- stale data
- command running
- command success
- command failure
- background refresh while editing
- narrow-width layout
- reduced-motion mode

The console should remain dense, useful, and operational. Do not add marketing
hero sections, decorative landing pages, or explanatory onboarding screens.

### Motion

Add only small functional motion:

- list/detail panel transitions
- command result appearance
- inline loading skeletons or shimmer-free skeletons
- focus-visible and hover transitions

Respect `prefers-reduced-motion`. No motion should block operation or hide
state changes.

### Release Readiness

Harden the GitHub tag/release path:

- Ensure `npm --prefix frontend/operator-console run build` writes renderer
  assets into `src/feishu_shadow_agent/console_static/`.
- Ensure `pyproject.toml` package data includes those built assets.
- Add or update release/testing docs with the exact build and validation steps.
- Validate an installable Python artifact can serve the bundled console assets.
- Document that GitHub Releases should attach source distribution and wheel
  artifacts built after the renderer build.
- Validate the built wheel contains `feishu_shadow_agent/console_static/index.html`
  and all referenced `/assets/*` files.

Recommended release validation commands:

```bash
rm -rf dist build
npm --prefix frontend/operator-console ci
npm --prefix frontend/operator-console run build
.venv/bin/python -m pytest -q
.venv/bin/python -m build
python3.11 -m venv /tmp/feishu-shadow-agent-release-check
/tmp/feishu-shadow-agent-release-check/bin/python -m pip install dist/*.whl
/tmp/feishu-shadow-agent-release-check/bin/python -m feishu_shadow_agent console --help
git diff --check
```

If `python -m build` is not available, P18 may add the `build` package to the
dev dependency set. Do not publish to PyPI as part of P18.

## Non-goals

- No remote console access.
- No account/login/user model.
- No GitHub Pages deployment.
- No Electron, Tauri, PyInstaller, or other binary packaging.
- No `config.yaml` write path.
- No `ConfigCommandService`.
- No risk levels or confirmation-required workflows.
- No raw log viewer as a primary screen.
- No direct SQLite reads from renderer code or route handlers.
- No replacement of the CLI operator commands.

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
frontend/operator-console/src/
frontend/operator-console/package.json
```

Packaging and docs:

```text
pyproject.toml
docs/testing.md
README.md
docs/configuration.md
```

Only update packaging metadata when the release validation actually requires it.

## Implementation Notes

### Health Issue Derivation

Keep health derivation inside `OperatorQueryService` or a query-owned helper.
The renderer should receive normalized issues and links, not table-specific
rows.

Recommended issue categories:

```text
daemon
policy
dispatch
store
runtime
```

Recommended severities:

```text
info
warning
error
critical
```

Severity should reflect operator urgency, not internal exception class names.

### Navigation Links

Health issue links should use existing screen/detail identifiers:

```text
task
message
approval
dispatch_action
policy
settings
```

If a target detail screen was deferred in P16 or P17, render the issue without a
dead link and leave a visible disabled action.

### Release Artifacts

The first public release target is:

```text
GitHub tag + GitHub Release + Python sdist/wheel with bundled renderer assets
```

Do not add GitHub Pages. Do not start binary packaging in this phase. Binary
packaging can be evaluated after the local console UI proves stable in normal
package form.

### Visual QA Evidence

Capture or record enough evidence to review the UI:

- desktop viewport
- narrow viewport
- at least one empty state
- at least one error state
- at least one command result state
- Health with at least one issue
- reduced-motion sanity check

If automated browser tooling is not added, manual screenshots are acceptable for
P18. Do not block release readiness on a large visual-test framework migration.

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

Release artifact validation:

```bash
.venv/bin/python -m build
```

Whitespace:

```bash
git diff --check
```

Browser verification:

- Dashboard and Health agree on high-signal issues.
- Health links open the intended detail screens.
- Failed dispatch actions show inspect/retry/cancel/mark-sent options only when
  valid.
- Policy uninitialized state appears as a policy health issue.
- Narrow-width layout keeps navigation and detail panels usable.
- Reduced-motion mode removes nonessential transitions.

## Acceptance

- `/api/health/issues` returns a token-protected, local-host-protected health
  DTO.
- Health issues are derived through query boundaries, not direct route SQL.
- Health screen is actionable and is not a raw log viewer.
- All final console screens have loading, empty, error, and command feedback
  behavior.
- Background refresh does not overwrite active edits.
- Visual QA covers desktop, narrow viewport, and reduced-motion behavior.
- Built renderer assets are included in the Python package artifact.
- Wheel contents include `console_static/index.html` and all referenced asset
  files, not stale files from a previous build.
- Release docs describe the GitHub tag/release artifact path.
- No GitHub Pages or binary packaging work is introduced.
- Frontend build passes.
- Relevant backend tests pass.
- Release artifact build passes or any missing build dependency is documented
  and added to dev dependencies.
- `git diff --check` passes.

## Handoff Notes

- P18 is a release-readiness phase, not an architecture expansion phase.
- Keep the console local-only.
- Keep GitHub Releases/tags as the distribution surface.
- Keep binary packaging as a future evaluation item.
- Keep health information normalized and actionable.
- Do not expose raw logs or arbitrary files through the local API.
