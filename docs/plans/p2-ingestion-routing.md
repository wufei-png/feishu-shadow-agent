# P2 Ingestion And Routing

## Summary

- Upgrade the P1 daemon from health-only/no-op ticks to the real P2 tick order:
  approval inbox placeholder, group `@me` ingest, P2P ingest, active watch, dispatch placeholder.
- P2 owns ingestion, normalization, resource status, checkpointing, task ownership, `watch_keys`,
  deterministic routing shortcuts, and owner takeover.
- P2 does not call Hermes, parse approval commands, compose replies, notify owner, or send external messages.

## Implementation Notes

- `FeishuClient` exposes business methods for message search, chat/thread listing, and bot resource download.
  `LarkCliClient` keeps command construction separate and maps JSON output to `MessagePage`.
- SQLite migration `0002_ingestion_routing` adds message routing fields, task watch fields, and `routing_audits`.
  Store APIs own all P2 writes for messages, resources, tasks, task messages, watch keys, approvals, actions,
  checkpoints, and route audit rows.
- `IngestionService` drains every page before advancing a checkpoint. Messages are processed in
  `create_time asc, message_id asc` order, and duplicate `message_id` rows are audited but not routed again.
- `MessageNormalizer` marks sender role, direct mention, `@all`, thread/reply target, mentions, text, and
  image/file resource metadata.
- `MessageRouter` uses SQLite-only candidate collection. P2P single active task, thread key, and `reply_to`
  message key are deterministic shortcuts. Other ambiguous cases record `router_placeholder` for P3.
- Owner messages are checked before normal routing. A uniquely related active task is marked
  `human_taken_over`, pending send actions are cancelled, and pending send approvals are expired.

## Validation

- Existing P1 tests remain green.
- P2 tests cover pagination drain, checkpoint rollback on failure, normalization, self/owner routing,
  deterministic shortcuts, duplicate suppression, resource download status, owner takeover, and daemon stage order.

## Acceptance

```bash
.venv/bin/python -m pytest
```
