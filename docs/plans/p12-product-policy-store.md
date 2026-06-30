# P12a Product Policy Store Foundation Plan

## Summary

P12a creates the durable Product Policy Store foundation: SQLite schema, store API, explicit config import/replace, and policy audit records. It does not cut runtime policy resolution over from YAML yet; that is P12b.

The goal is to leave a fresh implementation context with one well-tested product-policy persistence layer that later runtime, query, and command services can depend on.

## Background

Accepted decision: `docs/adr/0001-product-policy-store.md` makes Product Policy Store the future runtime source of truth. `config.yaml.reply_policy` and `config.yaml.chats` remain a Policy Import Source, not live runtime policy.

Current code still defines policy defaults in `src/feishu_shadow_agent/config.py` and resolves runtime policy through `PolicyResolver(config)` in `src/feishu_shadow_agent/policy.py`. P12a must not silently change that runtime behavior; it only builds the store and import boundary.

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/adr/0001-product-policy-store.md`
- `docs/plans/operator-surface-outline.md`
- `src/feishu_shadow_agent/config.py`
- `src/feishu_shadow_agent/policy.py`
- `src/feishu_shadow_agent/store/migrations/0001_foundation.sql`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/cli.py`

## Dependencies

- P10 and P11 are recommended first but not required by this storage slice.
- P12a must be complete before P12b, P13, and P14b.
- P12a must remain independently testable without Feishu or Hermes clients.

## Goals

- Add DB-backed Product Policy Store tables for global policy and per-chat policy.
- Reuse the existing `chat_policies` table for per-chat policy where practical.
- Add durable `policy_audits` for every inserted or replaced policy row.
- Add explicit CLI import commands:

```bash
python -m feishu_shadow_agent policy import-config --config config.yaml
python -m feishu_shadow_agent policy import-config --config config.yaml --replace
```

- Provide store/service methods and a product-policy initialization probe that can later feed runtime health.
- Preserve current runtime behavior until P12b cuts `PolicyResolver` over.

## Non-goals

- No runtime `PolicyResolver` cutover.
- No daemon fail-closed behavior yet; P12a may expose an initialization probe, but P12b decides runtime critical gating.
- No OperatorQueryService integration.
- No policy update UI or high-risk confirmation workflow.
- No deletion command.
- No old DB compatibility beyond the current clean-baseline migration style.
- No `context_access` changes.

## Product Policy Model

Global policy should contain all runtime defaults needed by policy resolution, not just the current YAML `reply_policy` fields:

```yaml
reply_policy:
  p2p_auto_reply: true
  unknown_group_auto_reply: false
default_chat_policy:
  bot_joined: false
  reply_identity: bot_preferred
  allow_user_fallback: true
  resource_download: true
```

Per-chat policy remains:

```yaml
chat_id: oc_xxx
name: 示例产品群
auto_reply: true
bot_joined: true
reply_identity: bot_preferred
allow_user_fallback: true
resource_download: true
```

`auto_reply` is per-chat. Unknown group auto-reply remains controlled by global `unknown_group_auto_reply`.

## Suggested Schema

Keep or evolve existing `chat_policies` as the per-chat policy table:

```text
chat_policies
  chat_id TEXT PRIMARY KEY
  name TEXT
  auto_reply INTEGER NOT NULL
  bot_joined INTEGER NOT NULL
  reply_identity TEXT NOT NULL
  allow_user_fallback INTEGER NOT NULL
  resource_download INTEGER NOT NULL
  updated_at TEXT NOT NULL
```

Add a global policy table, for example:

```text
product_policies
  key TEXT PRIMARY KEY          # e.g. reply_policy
  policy_json TEXT NOT NULL
  updated_at TEXT NOT NULL
```

Add audit:

```text
policy_audits
  id INTEGER PRIMARY KEY
  scope TEXT NOT NULL           # global | chat
  policy_key TEXT NOT NULL      # reply_policy | chat:<chat_id>
  actor TEXT NOT NULL           # local_cli | ui | import_config | system
  old_json TEXT
  new_json TEXT NOT NULL
  reason TEXT
  created_at TEXT NOT NULL
```

If implementation chooses different names, keep the Product Policy Store boundary and audit requirements intact.

## Import Semantics

Default `import-config`:

- Initialize global policy if DB missing.
- If config omits `reply_policy`, use Pydantic defaults and report `used_defaults: true`.
- Insert chat policies for `chat_id`s that do not exist in DB.
- Skip existing chat policies.
- Do not delete DB chat policies missing from config.
- Write `policy_audits` for inserted global/chat policies.
- Return structured YAML/JSON-friendly result fields such as `inserted`, `skipped`, `replaced`, `used_defaults`, and `audit_count`.

`--replace`:

- Replace global policy with config/default-derived value.
- Replace chat policies that appear in config.
- Do not delete DB chat policies missing from config.
- Write `policy_audits` for replaced global/chat policies.

Daemon startup must not import or synchronize policy automatically.

## Files To Update

- `src/feishu_shadow_agent/config.py`
- `src/feishu_shadow_agent/store/migrations/*.sql`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/cli.py`
- `docs/configuration.md`
- `docs/testing.md`
- `docs/specs/feishu-shadow-agent-mvp-design.md`
- `tests/test_config.py`
- `tests/test_store_migrations.py`
- `tests/test_cli.py`
- new focused Product Policy Store tests if the existing modules become too broad

## Test Plan

- New DB initializes Product Policy Store tables and policy audit table.
- Product Policy Store reports global policy missing before import.
- Product policy initialization probe reports missing before import and initialized after import.
- `policy import-config` creates global policy and missing chat policies.
- `policy import-config` without explicit `reply_policy` uses defaults and reports `used_defaults: true`.
- Default import skips existing chat policies and does not delete missing-from-config DB policies.
- `--replace` replaces global policy and config-listed chat policies.
- `--replace` does not delete DB chat policies absent from config.
- Policy import writes audit records with old/new values.
- Current `PolicyResolver(config)` behavior remains unchanged in P12a.
- Existing daemon and processing tests still pass before runtime cutover.

## Handoff Notes

- Do not add a live YAML/DB merge resolver. Two live policy sources are explicitly rejected by the ADR.
- Do not make daemon startup import policy automatically.
- Do not call the import-source comparison `config_drift`; the glossary term is Policy Import Diff.
- P12b owns runtime fail-closed and `PolicyResolver` cutover.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_config.py tests/test_store_migrations.py tests/test_cli.py
.venv/bin/python -m pytest -q tests/test_p2_ingestion_routing.py tests/test_p3_hermes_approval.py tests/test_daemon.py
.venv/bin/python -m pytest -q
git diff --check
```
