# Ingest caps defer work through an un-advanced checkpoint; never truncate silently

Status: accepted

High-traffic acquisition is bounded per chat (message/page cap) and per tick (global time budget), but a bounded Drain is never treated as success: when a chat's window is not fully fetched, the checkpoint is not advanced, the remainder becomes an Ingest Backlog, and the next tick re-fetches the tail with overlap and message-id/revision deduplication. Silently advancing the checkpoint past an undrained tail was considered and rejected because it would drop messages with no trace; the cost of recovering a lost message later is far higher than the cost of re-fetching a bounded tail. Observable lag (checkpoint age, page/message counts, drain completion, backlog markers) is exposed through Operator Query slices and JSONL logs, not only as daemon-internal state.
