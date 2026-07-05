# P18 Operator Console Visual QA Evidence

Date: 2026-07-01

Scope: P18 Health, cross-screen release readiness, and bundled renderer
validation. Evidence was captured with the local Python console server and the
Codex in-app browser against temporary SQLite/config fixtures under `/tmp`.
Temporary fixture paths and screenshots are not release artifacts and are not
committed.

## Health Issue Scenario

Fixture:

- Product Policy Store left uninitialized.
- One stale daemon run.
- One failed Hermes health check.
- One `failed_needs_review` dispatch action.
- One failed approval command.

Desktop viewport:

- Health rendered 5 open issues.
- Current issue list showed policy, daemon, runtime, dispatch, and approval
  command issues. This was historical P18 QA evidence; current Health semantics
  keep failed approval commands in recent summaries instead of open issue totals.
- Detail panel showed the selected policy issue with recommended action.
- Runtime panel showed daemon status `stale` and store `available`.
- Page text check confirmed no temporary database or log path appeared.

Narrow viewport:

- Viewport set to 390x820.
- Navigation collapsed to the horizontal icon rail.
- Work grid collapsed to a single column.
- DOM check reported no horizontal page overflow (`scrollWidth` matched viewport
  width).

Reduced motion:

- Browser media emulation set `prefers-reduced-motion: reduce`.
- DOM style check reported list-row and button transitions at `0s`.

Navigation:

- Dispatch health issue link opened `#dispatch/1`.
- Dispatch screen rendered action detail, readback summary, and valid recovery
  controls: Retry, Cancel, and Mark sent.
- After the dispatch control fix, a focused browser check confirmed
  `failed_needs_review` actions enable Retry, Cancel, and Mark sent when a sent
  message id is entered, while `failed` actions enable Retry and Cancel but keep
  Mark sent disabled.

## Shared State Checks

- Loading state: verified during local API-backed screen loads.
- Error state: verified by loading a console API-backed screen with an invalid
  token; the screen rendered the shared error state instead of stale data.
- Empty state: verified with a clean health fixture with initialized policy and
  live daemon heartbeat; Health rendered `No open health issues`.
- Command result state: verified by running the Dashboard approval expiry command
  in the temporary fixture; the shared command result panel rendered the backend
  `CommandResult`.

## Release Artifact Checks

Validation commands:

```bash
npm --prefix frontend/operator-console run build
.venv/bin/python -m pytest -q
.venv/bin/python -m build
```

Wheel inspection confirmed:

- `feishu_shadow_agent/console_static/index.html` is present.
- Referenced `/assets/index-B4ev8NH8.css` is present.
- Referenced `/assets/index-D7sTUODg.js` is present.

Known environment note:

- `python3.11 -m venv /tmp/feishu-shadow-agent-release-check` failed on this
  machine before package installation because the host `python3.11` venv could
  not complete `ensurepip`. The same wheel installed and ran `console --help`
  successfully in an isolated venv created with `.venv/bin/python -m venv`.
