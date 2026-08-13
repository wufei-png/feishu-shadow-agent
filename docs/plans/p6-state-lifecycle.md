# P6 State And Lifecycle Plan

## Summary

P6 rebuilds the state model as a clean baseline. The project is not yet in production, so this plan does not preserve old SQLite compatibility. It introduces Python `StrEnum` values, DB `CHECK` constraints, task lifecycle/blocker separation, global lifecycle config, and approval expiry.

## Goals

- Rewrite migrations into a clean current schema baseline.
- Replace loose status strings with Python `StrEnum` constants.
- Add DB `CHECK` allowlists for status/kind/route/stage fields.
- Remove `waiting_approval` from `tasks.status`.
- Keep approval as blocker state derived from `approvals`, not task lifecycle.
- Add global lifecycle config.
- Add nullable `approvals.expires_at`.

## Non-goals

- No old DB upgrade compatibility.
- No per-chat lifecycle config.
- No multi owner or delegated approval actor model.
- No dispatch attempt schema; P7 owns it.
- No TaskProcessingService broad refactor; P9 owns remaining split. P6 may add narrow lifecycle/state helpers only where needed to keep state rules out of prompts and ad hoc strings.

## Migration Policy

Hard constraint:

```text
Implementation may rewrite src/feishu_shadow_agent/store/migrations/.
Developers must delete old data/agent.sqlite3 before running the new version.
Tests only need to cover empty/new database initialization.
```

Remove legacy-only migration concepts such as:

```text
hermes_session_id -> agent_session_id compatibility
legacy feishu-task-* session cleanup
old default_group_auto_reply compatibility
waiting_approval task status compatibility
```

## Python Enum Source

Introduce runtime enum constants, preferably in `src/feishu_shadow_agent/types.py` or a new small state module:

```python
from enum import StrEnum
```

Required values:

```text
TaskStatus:
  watching
  closed
  closed_by_owner
  human_taken_over

ApprovalKind:
  send_reply
  tool_action

ApprovalStatus:
  pending
  approved
  rejected
  expired

ActionKind:
  send_reply
  owner_notification

ActionStatus:
  pending
  sending
  sent
  failed
  failed_needs_review
  cancelled

RouteName:
  new_task
  attach_task
  reopen_task
  close_task
  ignore
  ambiguous
  human_taken_over

MessageProcessingStage:
  task_router
  task_session
  resource_download

MessageProcessingStatus:
  processed
  processing_failed_terminal
  blocked_waiting_external

ResourceStatus:
  downloaded
  skipped
  bot_not_joined
  bot_invisible
  failed
  missing_file
  too_large
  quota_exceeded
  expired
```

Migration SQL remains hand-written. Tests must prevent enum/schema drift by inserting allowed values and rejecting invalid values.

## DB CHECK Requirements

Add strict `CHECK` constraints at least for:

- `tasks.status`
- `approvals.kind`
- `approvals.status`
- `actions.kind`
- `actions.status`
- `resources.download_status`
- `routing_audits.route`
- `message_processing.stage`
- `message_processing.status`
- `messages.chat_type`
- `messages.sender_role`

Do not allow future or temporary strings in DB. Adding a new status must update enum, migration, tests, and relevant logic together.

## Task Lifecycle Model

New task lifecycle:

```text
tasks.status:
  watching
  closed
  closed_by_owner
  human_taken_over
```

Approval blocker:

```text
has_pending_approval = exists approvals where task_id = ? and status = pending
```

Rules:

- Creating a send-reply approval does not change `tasks.status`.
- A task that needs owner approval remains `watching` unless another rule closes it.
- Active watch only selects `tasks.status = watching`.
- Only pending approvals are blockers. `approved`, `rejected`, and `expired` approvals are historical state.
- Owner reject closes the task.
- Owner approve/send keeps the task `watching` unless a separate close rule applies.
- `human_taken_over` closes the task and expires pending approvals/cancels pending sends.

## Lifecycle Config

Add global config only:

```yaml
lifecycle:
  watch_minutes: 120
  closed_recall_days: 7
  approval_timeout_hours: 24  # null means approvals never expire
```

No per-chat lifecycle in P6.

`watch_minutes` replaces hard-coded `WATCH_EXTEND_MINUTES`.

`closed_recall_days` replaces hard-coded 7-day recall windows.

`approval_timeout_hours` writes `approvals.expires_at` at creation time:

```text
if approval_timeout_hours is null:
  expires_at = NULL
else:
  expires_at = created_at + approval_timeout_hours
```

Default remains `24`.

## Approval Expiry

Add:

```text
approvals.expires_at TEXT NULL
```

Pending approval expiry is code-owned:

```text
pending approval where expires_at is not null and expires_at < now -> expired
```

Required first-version behavior for send-reply approval expiry:

- mark approval `expired`
- set `approvals.resolved_at = now`
- cancel related pending send actions for the same task/target if any exist
- keep `tasks.status = watching`
- record enough status/audit data for `status` and `replay`

Approval expiry must not:

- close the task
- trigger a task-session agent call by itself
- insert a synthetic row into `messages` / `task_messages`
- send an `expired` event into the resumed external agent session

The task-session prompt should also make the source-of-truth boundary explicit:

```text
Only messages in the messages block are real Feishu messages. Previous proposed_reply was not sent unless a sent action or real message shows it.
```

Do not add a persistent `task_state` field or table in P6. If a future prompt card is needed, it should be a per-call derived snapshot of current blockers only; expired approvals should remain visible through `status`, `replay`, audit data, and explicit operator views rather than repeated task-session prompts.

## Blocked External Processing

Use:

```text
message_processing.status = blocked_waiting_external
```

for recoverable external blockers such as:

- `resource_needs_bot`
- `resource_too_large`
- `resource_quota_exceeded`
- `resource_download_disabled`

Duplicate ingest behavior:

- `processed`: do not rerun.
- `processing_failed_terminal`: do not rerun automatically.
- `blocked_waiting_external`: do not rerun on ordinary duplicate ingest, but allow explicit retry/replay flows.

## Minimal Helper Boundaries

Keep P6's structural extraction narrow and tied to the state model:

```text
LifecycleStatePolicy:
  owns allowed task status transitions
  derives approval/blocker state from approvals/actions
  applies approval expiry task/action consequences
  keeps expired approvals out of active blockers and task-session prompt events
  maps blocked_waiting_external retry vs duplicate behavior

StateSchemaContract:
  exposes enum allowlists for migration/schema tests
  prevents Python enum and DB CHECK drift
```

Do not move routing, dispatch recovery, resource quotas, or broad task-session orchestration into these helpers. Those belong to P5, P7, P8, and P9 respectively.

## Files To Update

- `src/feishu_shadow_agent/types.py`
- `src/feishu_shadow_agent/config.py`
- `src/feishu_shadow_agent/store/migrations/*.sql`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/ingestion.py`
- `src/feishu_shadow_agent/routing.py`
- `src/feishu_shadow_agent/processing.py`
- `src/feishu_shadow_agent/dispatcher.py`
- `src/feishu_shadow_agent/prompt.py`
- `config.example.yaml`
- `schemas/config.schema.json`
- `docs/configuration.md`
- `docs/specs/feishu-shadow-agent-mvp-design.md`
- `tests/test_store_migrations.py`
- `tests/test_config.py`
- scenario tests touching approval/task/action states

## Test Plan

- Empty database migrate creates complete schema in one clean baseline.
- Each enum value can be inserted where valid.
- Invalid status/kind/route/stage strings fail DB `CHECK`.
- `waiting_approval` is not accepted in `tasks.status`.
- Creating approval does not change `tasks.status`.
- Pending approval appears in status/replay through derived blocker data.
- Owner reject closes task.
- Owner approve/send leaves task `watching`.
- Approval expiry writes `expired`, sets `resolved_at`, cancels related pending sends, and leaves task `watching`.
- Expired approvals do not appear as active blockers and do not trigger task-session calls.
- Task-session prompt states that previous `proposed_reply` was not sent unless a sent action or real message shows it.
- `approval_timeout_hours: null` produces `expires_at = NULL`.
- `watch_minutes` and `closed_recall_days` replace hard-coded values in tests.
- `blocked_waiting_external` is used for resource blockers.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_config.py tests/test_store_migrations.py
.venv/bin/python -m pytest -q tests/test_p2_ingestion_routing.py tests/test_p3_hermes_approval.py tests/test_dispatcher.py tests/test_daemon.py
.venv/bin/python -m pytest -q
git diff --check
```
