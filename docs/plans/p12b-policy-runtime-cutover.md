# P12b Policy Runtime Cutover Plan

## Summary

P12b moves runtime policy resolution from `config.yaml.reply_policy` / `config.yaml.chats` to Product Policy Store. After this phase, daemon processing, resource preflight, reply gates, and UI-facing policy summaries read Product Policy from SQLite.

P12b assumes P12a has already provided store schema, import/replace commands, and policy audit records.

## Background

Current runtime policy is config-backed:

- `PolicyResolver` is constructed from `AppConfig`.
- `PolicyResolver.resolve_chat_policy()` reads `config.chats`, `config.reply_policy.p2p_auto_reply`, and `config.reply_policy.unknown_group_auto_reply`.
- `IngestionService` and `TaskProcessingService` instantiate `PolicyResolver(config)`.

The accepted Product Policy Store decision requires a clean source-of-truth switch: YAML policy fields remain a Policy Import Source only. The daemon must fail closed if DB global policy is not initialized, because otherwise runtime behavior would silently fall back to stale config.

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/adr/0001-product-policy-store.md`
- `docs/plans/operator-surface-outline.md`
- `docs/plans/p12-product-policy-store.md`
- `src/feishu_shadow_agent/policy.py`
- `src/feishu_shadow_agent/ingestion.py`
- `src/feishu_shadow_agent/processing.py`
- `src/feishu_shadow_agent/daemon.py`
- `src/feishu_shadow_agent/health.py`
- `tests/test_p2_ingestion_routing.py`
- `tests/test_p3_hermes_approval.py`

## Dependencies

- P12a Product Policy Store Foundation must be complete.
- P11 is recommended first so later query paths do not inherit read-side mutations.
- P12b must be complete before P13 and P14b.

## Goals

- Move runtime `PolicyResolver` off direct `config.reply_policy` / `config.chats` reads.
- Make `PolicyResolver` read Product Policy Store through an explicit policy repository/service boundary.
- Preserve existing effective policy behavior after importing the same config into DB.
- Make missing DB global product policy a critical runtime health failure.
- Make daemon processing fail closed when global product policy is missing.
- Keep config policy fields available only for explicit import/replace.
- Include enough `policy_source` detail for future query outputs to explain explicit chat policy vs global defaults.
- Update docs/specs so they no longer say runtime policy is primarily `config.yaml`.

## Non-goals

- No new policy mutation commands beyond P12a import/replace.
- No Web UI or HTTP server.
- No OperatorQueryService implementation.
- No direct policy update commands.
- No deletion command.
- No multi-owner or permission account model.
- No `context_access` changes.

## Runtime Semantics

- DB global policy missing is fail-closed:
  - `doctor` / runtime critical health reports a critical product-policy failure.
  - daemon does not process task work or actual sends.
  - error output should tell the operator to run `policy import-config`.
- Missing per-chat policy is not an error:
  - resolver uses global policy and default chat policy.
  - P2P auto-reply comes from global `reply_policy.p2p_auto_reply`.
  - unknown group auto-reply comes from global `reply_policy.unknown_group_auto_reply`.
- Effective policy should preserve existing behavior when DB is seeded from the same config.
- Runtime code must not silently synchronize DB from YAML.

## Files To Update

- `src/feishu_shadow_agent/policy.py`
- `src/feishu_shadow_agent/ingestion.py`
- `src/feishu_shadow_agent/processing.py`
- `src/feishu_shadow_agent/daemon.py`
- `src/feishu_shadow_agent/health.py`
- `src/feishu_shadow_agent/cli.py` if runtime bootstrap helpers live there
- `docs/configuration.md`
- `docs/testing.md`
- `docs/specs/feishu-shadow-agent-mvp-design.md`
- `tests/test_p2_ingestion_routing.py`
- `tests/test_p3_hermes_approval.py`
- `tests/test_daemon.py`
- policy-focused tests for effective policy equivalence

## Test Plan

- Runtime health fails when global product policy is missing.
- Daemon exits or skips work fail-closed when global product policy is missing.
- After `policy import-config`, daemon and processing use DB policy.
- `PolicyResolver` does not read `config.reply_policy` / `config.chats` during runtime resolution.
- Effective policy after importing the same config matches pre-cutover behavior for:
  - P2P auto-reply enabled/disabled
  - unknown group auto-reply enabled/disabled
  - explicit chat auto-reply
  - bot joined vs bot not joined
  - reply identity and user fallback
  - resource download allowed/blocked
- Missing per-chat DB policy uses global/default policy instead of failing.
- Docs no longer describe `config.yaml` as the runtime policy source.

## Handoff Notes

- Do not keep a fallback from missing DB policy to YAML policy. That recreates two live policy sources.
- Do not remove config fields; they remain the explicit Policy Import Source.
- P13 owns read-only UI DTOs and Policy Import Diff display.
- P14b owns arbitrary policy updates and policy audit records.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_p2_ingestion_routing.py tests/test_p3_hermes_approval.py tests/test_daemon.py
.venv/bin/python -m pytest -q tests/test_cli.py tests/test_store_migrations.py
.venv/bin/python -m pytest -q
git diff --check
```
