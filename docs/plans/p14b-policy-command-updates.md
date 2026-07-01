# P14b Policy Command Updates Plan

## Summary

P14b adds Product Policy mutation commands behind `OperatorCommandService`. It wraps P12a import/replace behavior in the command facade, adds direct global/chat policy updates, and writes policy audits for every applied mutation.

This is the policy-mutation companion to P14a. It assumes runtime policy already reads from Product Policy Store.

## Background

Policy changes can immediately change real auto-reply, resource-download, and identity-fallback behavior. Because Product Policy Store is the runtime source of truth after P12b, UI and CLI policy edits must be auditable. The backend records old/new policy and actor/reason, but it does not risk-rank legal setting changes or require a second confirmation step.

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/adr/0001-product-policy-store.md`
- `docs/plans/operator-surface-outline.md`
- `docs/plans/p12-product-policy-store.md`
- `docs/plans/p12b-policy-runtime-cutover.md`
- `docs/plans/p13-operator-query-service.md`
- `docs/plans/p14-operator-command-service.md`
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/policy.py`
- Product Policy Store implementation from P12a
- OperatorCommandService implementation from P14a

## Dependencies

- P12a must be complete for store/import/audit.
- P12b must be complete so DB policy is live runtime policy.
- P13 should be complete for policy status and Policy Import Diff DTOs.
- P14a must be complete for the command facade and result shape.

## Goals

- Add `PolicyCommandService` behind `OperatorCommandService`.
- Route `policy import-config` and `policy import-config --replace` through `PolicyCommandService`.
- Add direct global policy update command(s).
- Add direct chat policy update command(s).
- Write `policy_audits` for every mutation.
- Return structured command results suitable for UI.
- Apply valid policy changes directly after schema validation.
- Keep update command APIs independent from CLI parsing and UI rendering.

## Non-goals

- No Web UI implementation.
- No multi owner or authentication model.
- No policy deletion command.
- No Feishu interactive card commands.
- No live YAML/DB merge mode.
- No `context_access` changes.

## Policy Update Behavior

Direct update commands mutate only Product Policy Store, never `config.yaml`.

All valid changes, including automation, resource download, bot joined, reply identity, and user fallback expansions, apply directly and write `policy_audits`. Results include old/new policy and audit metadata. `warnings` remains part of the shared command result shape for technical non-blocking warnings, but policy expansion is not reported as a risk warning.

Future UI settings should explain each field with labels, descriptions, and hover/help text. The backend should not classify legal settings as high/low risk or prevent the owner from applying them after they are valid.

## Command Result Shape

Use the P14a common fields:

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

Additional policy fields:

```text
old_policy
new_policy
audit_id or audit_count
policy_import_diff
```

## Files To Update

- `src/feishu_shadow_agent/operator_commands.py` or equivalent command module from P14a
- Product Policy Store/service module from P12a
- `src/feishu_shadow_agent/cli.py`
- `docs/configuration.md`
- `docs/testing.md`
- `docs/specs/feishu-shadow-agent-mvp-design.md`
- `tests/test_cli.py`
- Product Policy Store tests
- operator command tests

## Test Plan

- `policy import-config` goes through `PolicyCommandService`.
- `policy import-config --replace` goes through `PolicyCommandService`.
- Global policy update writes DB policy and `policy_audits`.
- Chat policy update writes DB policy and `policy_audits`.
- Automation/resource/fallback expansion updates mutate DB directly and write audit.
- Narrowing updates mutate DB directly and write audit.
- Actor and reason are persisted in audit rows.
- Command result payloads include old/new policy summaries and next actions.
- Query paths still do not mutate state.

## Handoff Notes

- Do not write `config.yaml`; policy commands mutate Product Policy Store.
- Do not call Policy Import Diff `config_drift`.
- Do not bypass `OperatorCommandService` for UI-facing policy mutations.
- Do not treat `policy_audits` as logs; they are product records.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_cli.py
.venv/bin/python -m pytest -q tests/test_store_migrations.py
.venv/bin/python -m pytest -q
git diff --check
```
