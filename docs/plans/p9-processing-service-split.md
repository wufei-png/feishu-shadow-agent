# P9 Processing Service Split Plan

## Summary

P9 is the final structural cleanup after P5-P8 have stabilized behavior. It should reduce `TaskProcessingService` size and responsibility without changing product semantics.

## Goals

- Keep `TaskProcessingService` as the high-level orchestrator.
- Extract remaining stable responsibilities into focused collaborators.
- Preserve prompt contracts, reply gates, resource preflight semantics, and audit behavior.
- Make future behavior changes easier to review.

## Non-goals

- No schema redesign.
- No policy semantic changes.
- No routing semantic changes.
- No dispatch recovery changes.
- No new agent backend/provider.
- No broad store rewrite.

## Expected Precondition

P9 assumes previous plans have already landed:

- P5 introduced `PolicyResolver`.
- P6 introduced `StrEnum` state values and clean DB schema.
- P7 introduced dispatch attempts/recovery.
- P8 introduced resource quota and heartbeat.

If those plans have not landed, do not start P9 as a pure refactor. Apply the required behavior plan first.

## Target Splits

Recommended collaborators:

```text
AgentInvoker:
  agent call retries
  retry classification
  latency/error logging helper

ContextAccessBuilder:
  router context_access
  task session context_access
  query scope card construction

ResourcePreflight:
  resource status inspection
  retry attempts through injected resource retry function
  blocked_waiting_external mapping

TaskSessionRunner:
  prompt message id selection
  prompt construction call
  output model selection
  schema validation wrapper
```

`TaskProcessingService` should still own:

```text
route dispatch at a high level
router/session orchestration
deciding which collaborator to call
creating final ProcessingResult
```

Do not extract everything at once if it makes review harder. Extract one stable boundary per commit if needed.

## Store Boundary

P9 should not rewrite `SQLiteStore` wholesale. If a store method is too broad, only add narrower wrapper/helper methods required by the extracted service.

Approval/action transaction boundaries should remain in store methods unless a future dedicated plan moves them.

## Context Access Safety

`ContextAccessBuilder` should preserve current product boundary:

- Python decides whether any context access is exposed.
- Prompt may use provided context semantically.
- Prompt must not be treated as the security boundary for local side effects.

If P9 finds context access still too prompt-enforced, document a follow-up plan for Python-owned context snapshots or read-only query APIs. Do not broaden access in P9.

## Files To Update

Likely files:

- `src/feishu_shadow_agent/processing.py`
- `src/feishu_shadow_agent/policy.py`
- new modules under `src/feishu_shadow_agent/` as needed, such as:
  - `agent_invocation.py`
  - `context_access.py`
  - `resource_preflight.py`
- `tests/test_p3_hermes_approval.py`
- new focused tests for extracted collaborators if useful

Avoid moving test helpers broadly unless necessary. Stable fake extraction can happen, but should remain mechanical.

## Test Plan

- Existing P2/P3/daemon/dispatcher tests remain behaviorally unchanged.
- Add focused unit tests for extracted helpers only where they reduce scenario-test burden.
- Verify retry classification remains identical.
- Verify context_access cards remain identical for router/task session.
- Verify resource preflight decisions remain identical.
- Verify schema failure and owner notification paths are unchanged.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_p3_hermes_approval.py
.venv/bin/python -m pytest -q
git diff --check
```
