# P14a Operator Command Service Facade Plan

## Summary

P14a introduces `OperatorCommandService` as the mutation-side companion to `OperatorQueryService`. This phase wraps existing approval, dispatch recovery, and maintenance commands behind a stable command facade and structured command result shape.

P14a deliberately does not add arbitrary policy update commands. Policy import already exists from P12a, and broader policy mutation/risk confirmation is P14b.

## Background

Current CLI handlers call store or dispatcher methods directly:

- `approve`, `reject`, and `send` call `store.apply_approval_command()`.
- `dispatch inspect` calls `store.get_dispatch_inspection()`.
- `dispatch mark-sent` goes through `Dispatcher.mark_action_sent_after_readback()`.
- `dispatch retry` / `cancel` call store recovery methods.
- P11 adds explicit maintenance approval expiry.

These commands already carry important transactional and idempotency behavior. P14a should preserve that behavior while creating one UI-ready command boundary.

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/plans/operator-surface-outline.md`
- `docs/plans/p11-approval-expiry-boundary.md`
- `docs/plans/p13-operator-query-service.md`
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/dispatcher.py`
- `src/feishu_shadow_agent/processing.py`

## Dependencies

- P11 must be complete so maintenance expiry exists and query paths do not write.
- P13 should be complete so command result language can align with operator DTOs.
- P14b depends on P14a.

## Goals

- Add `OperatorCommandService` as the single mutation facade for operator actions.
- Split implementation behind the facade into focused services:
  - `ApprovalCommandService`
  - `DispatchCommandService`
  - `MaintenanceCommandService`
- Move CLI approve/reject/send/dispatch recovery/maintenance expiry through the command facade.
- Return structured command results suitable for CLI YAML and future UI JSON/cards.
- Preserve existing transactional safety and idempotency.
- Keep command APIs independent from argparse and CLI formatting.

## Non-goals

- No Web UI implementation.
- No policy update commands.
- No high-risk policy confirmation.
- No multi owner or authentication model.
- No Feishu interactive card commands.
- No automatic dispatch resend.
- No broad Store rewrite or state-machine redesign.

## Service Responsibilities

`ApprovalCommandService`:

- approve approval id or unambiguous task id
- reject approval id or unambiguous task id
- send final reply for task
- run approval expiry before command application, preserving existing P11 semantics

`DispatchCommandService`:

- inspect action and attempts
- mark sent with readback evidence
- retry failed / failed_needs_review action
- cancel action

`MaintenanceCommandService`:

- expire overdue approvals explicitly
- expose future maintenance commands without mixing them into read queries

`OperatorCommandService`:

- route commands to the correct subservice
- normalize actor/reason/command result shape
- keep command APIs independent from CLI argument parsing
- return data, not preformatted human text

## Command Result Shape

Recommended common fields:

```text
status
command
actor
target
changed
result
warnings
next_actions
```

Recommended statuses:

```text
applied
no_change
failed
not_found
conflict
validation_failed
```

CLI can render YAML; future UI can render JSON/cards. The facade should not print.

## Files To Update

- `src/feishu_shadow_agent/operator_commands.py` or equivalent new module
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/store/sqlite_store.py` only for narrow helper extraction if needed
- `src/feishu_shadow_agent/dispatcher.py` if needed for dependency injection
- `docs/testing.md`
- `docs/configuration.md`
- `tests/test_cli.py`
- `tests/test_dispatcher.py`
- `tests/test_p3_hermes_approval.py`
- new focused operator command tests

## Implementation Notes

- Prefer wrapping existing store/dispatcher methods over moving transaction logic into the facade.
- Preserve current CLI exit-code semantics unless tests show a product reason to change them.
- Keep `dispatch inspect` read-only even though it sits under the command facade; it belongs here because operators think of it as part of dispatch recovery workflow.
- Do not introduce policy mutation in P14a.

## Test Plan

- CLI approve/reject/send behavior is unchanged through `OperatorCommandService`.
- CLI dispatch inspect/mark-sent/retry/cancel behavior is unchanged through `OperatorCommandService`.
- Explicit maintenance expiry command advances overdue approvals and reports count.
- Command result payloads are structured and stable.
- Facade methods do not depend on argparse objects.
- Query paths still do not mutate state.
- Existing dispatch idempotency and readback evidence requirements remain intact.

## Handoff Notes

- This phase is a facade and result-shape phase, not a state-machine rewrite.
- Do not move approval expiry into QueryService.
- Do not make `retry` automatically send; it only requeues according to existing dispatch recovery semantics.
- P14b owns policy command/update/risk confirmation.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_cli.py tests/test_dispatcher.py tests/test_p3_hermes_approval.py
.venv/bin/python -m pytest -q
git diff --check
```
