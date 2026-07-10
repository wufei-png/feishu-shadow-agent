# Rebuild evaluation state in Temporary Eval Stores

Status: accepted

Router, task-session, and full-chain evaluation will run against a per-case Temporary Eval Store rebuilt from explicit scenario inputs. The production store may be queried read-only while authoring a case, but no production SQLite snapshot is copied into eval artifacts: once a target message has changed task fields, audits, actions, or a provider-owned session, deleting its rows cannot reliably reconstruct the state that existed before the target, and a full copy would retain unrelated private data.

## Consequences

Evaluation runners may use the production SQLite schema and store APIs, but must never mutate, copy, or directly execute against the production store. Capture exports only the task fixtures, watch keys, and raw messages explicitly selected for the Evaluation Scenario. Router scenarios seed only the candidate state needed before the target; task-session scenarios seed explicit task membership and establish resume sessions through real setup turns; full-chain scenarios replay explicit setup messages before scoring the target. File-backed SQLite remains necessary because production code opens multiple connections and agent backends may need cross-process read-only context access. A locked per-case access alias keeps that URI stable without reusing the underlying temporary database.

Each turn uses an Evaluation Clock derived from that scenario message's `sent_at`. Golden promotion rejects missing, invalid, or out-of-order timestamps instead of falling back to the machine clock, so lifecycle and routing results remain stable across later replays.

Messages with files or images use explicit Evaluation Resource Fixtures rather than production resource rows or live download fallback. Capture stores successfully downloaded bytes and SHA-256 values; every trial copies those bytes into its own resource directory and rebuilds production-format resource state. Cases with unresolved resources remain drafts and cannot be promoted.
