# P8 Runtime And Resource Safety Plan

## Summary

P8 improves runtime operability and local resource safety. It adds daemon heartbeat fields, SQLite busy timeout with WAL evaluation, and resource download quotas. It does not change default tool permissions.

## Goals

- Record daemon liveness in `runs`.
- Let `status` distinguish a live daemon from a stale/crashed run.
- Add SQLite `busy_timeout`.
- Evaluate WAL and enable it only if suitable for this local-file deployment.
- Add per-file and total resource directory quotas.
- Mark oversized/quota-blocked resources with explicit statuses.

## Non-goals

- No default `tool_permissions` change.
- No full observability redaction redesign.
- No chat/resource capability doctor probing unless needed by quota implementation.
- No retry scheduler.
- No automatic dispatch resend.
- No per-chat lifecycle.

## Daemon Heartbeat

Add fields to `runs`:

```text
last_heartbeat_at TEXT
last_tick_started_at TEXT
last_tick_finished_at TEXT
last_tick_status TEXT CHECK (
  last_tick_status IS NULL OR last_tick_status IN (
    'running',
    'ok',
    'partial_failed',
    'failed'
  )
)
last_tick_summary_json TEXT NOT NULL DEFAULT '{}'
```

Add matching Python enum values:

```text
RunTickStatus:
  running
  ok
  partial_failed
  failed
```

The DB `CHECK` allowlist and Python enum must stay aligned through tests.

No `run_stages` history table in P8. JSONL remains the detailed event history; `runs` should answer current operator questions.

Tick semantics:

```text
tick start:
  last_heartbeat_at = now
  last_tick_started_at = now
  last_tick_status = running

after each stage:
  update last_heartbeat_at
  accumulate stage summary

tick finish:
  last_tick_finished_at = now
  last_tick_status = ok | partial_failed | failed
  last_tick_summary_json = stage results
```

`status` should flag a daemon as stale when the latest run is `running` but `last_heartbeat_at` is older than a configured or fixed threshold.

## SQLite Concurrency

In `SQLiteStore.connect()`:

```text
PRAGMA foreign_keys = ON
PRAGMA busy_timeout = <configured or fixed milliseconds>
```

Recommended first value:

```text
busy_timeout = 5000 ms
```

WAL evaluation:

- Evaluate enabling `PRAGMA journal_mode=WAL` for local filesystem use.
- If implementation enables WAL, add tests and docs explaining generated `-wal` / `-shm` files.
- If implementation leaves WAL off, document why and keep busy timeout as the accepted improvement.

Do not block P8 completion on WAL if it introduces portability or cleanup risk.

## Resource Quota Config

Add storage config fields:

```yaml
storage:
  max_resource_bytes: 52428800        # 50 MiB per downloaded resource
  max_resource_dir_bytes: 2147483648  # 2 GiB total resource directory budget
```

These defaults are intended to be open enough for normal screenshots/files while still protecting disk space. If implementation chooses different defaults, update docs and tests explicitly.

## Resource Quota Semantics

Resource statuses include:

```text
too_large
quota_exceeded
```

Per-file:

```text
after download, stat file
if size > max_resource_bytes:
  delete file
  resources.download_status = too_large
  resources.path = NULL
  keep attempted path/detail only in diagnostic metadata or result JSON
```

Total directory:

```text
before or after download, compute resource_dir usage
if usage would exceed or does exceed max_resource_dir_bytes:
  if this tick already wrote a file, delete that just-downloaded file
  do not continue downloading
  resources.download_status = quota_exceeded
  resources.path = NULL
```

Task session preflight:

```text
too_large or quota_exceeded -> blocked_waiting_external
notify owner
do not call task session agent for that message by default
```

First version should not attempt a semantic fallback where the agent answers without the resource. If a message has a resource that could not be safely provided, owner review is safer.

## Minimal Helper Boundaries

Keep P8's structural extraction narrow and tied to the new behavior:

```text
HeartbeatRecorder:
  writes run heartbeat fields
  summarizes tick outcome for status

ResourceQuotaGuard:
  checks per-file and total-dir quotas
  deletes blocked files
  maps quota outcomes to resource status and blocked_waiting_external
```

Do not use P8 as a broad `TaskProcessingService` cleanup. P9 owns the remaining service split.

## Files To Update

- `src/feishu_shadow_agent/types.py`
- `src/feishu_shadow_agent/config.py`
- `src/feishu_shadow_agent/store/migrations/*.sql`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/daemon.py`
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/ingestion.py`
- `src/feishu_shadow_agent/processing.py`
- `src/feishu_shadow_agent/retention.py`
- `config.example.yaml`
- `schemas/config.schema.json`
- `docs/configuration.md`
- `docs/testing.md`
- `tests/test_config.py`
- `tests/test_store_migrations.py`
- `tests/test_daemon.py`
- `tests/test_retention.py`
- `tests/test_p2_ingestion_routing.py`
- `tests/test_p3_hermes_approval.py`

## Test Plan

- Daemon tick writes heartbeat at start and finish.
- Stage failures produce `partial_failed` or `failed` summary.
- `status` reports stale daemon when heartbeat is old.
- Python `RunTickStatus` enum values match DB `CHECK` values.
- Invalid run tick status fails DB `CHECK`.
- SQLite connections apply `busy_timeout`.
- WAL behavior is either enabled and tested, or documented as intentionally deferred.
- Per-file oversized download is deleted, marked `too_large`, and leaves `resources.path = NULL`.
- Total resource directory quota blocks further download, deletes any just-downloaded file, marks `quota_exceeded`, and leaves `resources.path = NULL`.
- `too_large` / `quota_exceeded` block task session and notify owner.
- `resource_download` remains independent from `unknown_group_auto_reply`.
- Existing retention does not break on `too_large` or `quota_exceeded` rows.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_config.py tests/test_daemon.py tests/test_retention.py
.venv/bin/python -m pytest -q tests/test_p2_ingestion_routing.py tests/test_p3_hermes_approval.py
.venv/bin/python -m pytest -q
git diff --check
```
