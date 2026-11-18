# Single-group AI coworker

**Revision:** Regular group main messages share a group session, while topic messages share a topic session. The default send mode is agent direct; project dispatch remains configurable. The mode is fixed at startup, and changing it retains the provider session while injecting the current instructions. Agent-direct delegation uses task IDs and mutual mentions without project-side timeout or loop tracking. Existing imported policies without a send mode retain project dispatch until explicitly reimported. This revision supersedes conflicting details below.

**Process isolation revision:** Each daemon launch selects either shadow or coworker business mode, fixed until restart. Shadow ingestion and retries do not run in coworker mode; project-owned sends and approval callbacks are scoped to the running mode. Suspended approvals keep their wall-clock deadlines and are marked expired when their mode resumes. Separately launched processes are not globally locked.

**Agent autonomy revision:** Each turn supplies the current message, identifiers, history access and the shared memory path; the agent chooses whether to read history or memory. Immediate memory updates are agent-owned. Scheduled consolidation starts a separate fresh agent session, which may search across topics. Native tool writes and direct sends are not verified by the project. These decisions supersede the earlier structured-memory and project-owned delegation descriptions below.

**Native subagent revision:** Codex coworker sessions use a persistent App Server. The main agent decides whether to answer, spawn a native subagent, or interact with an existing one. Root turns remain serialized per group/topic while different topics may run concurrently. Follow-ups can steer an active turn; durable child completion events wake the same root session. Ambiguous results require review rather than replay. Hermes does not implement this App Server integration.

Status: accepted for the opt-in test mode; live Feishu interoperability remains to be verified.

## Decision

The project receives Feishu events and owns sessions, policy, approvals and its dispatch path. One configured group can use the public AI coworker role, with group-shared or topic-shared provider sessions and independently configured trigger and history source. Codex is the default coworker provider and Hermes is selectable. Shadow ingestion does not run in the coworker daemon.

Add `inherit` to the existing tool-permission profiles, superseding the two-profile restriction in [ADR 0002](0002-supported-agent-backends-must-be-complete.md). It omits project-specified permission overrides; effective capability depends on the selected provider's environment. Coworker calls explicitly use native provider configuration and context. Project-owned sends retain policy, approval, dry-run, idempotency and dispatch gates; provider tools run with their effective native permissions and can independently access external systems. This makes the scope of [ADR 0012](0012-agent-prompt-contracts-and-traceability.md) explicit.

Use a single event connection for messages and approval cards. Persist message events synchronously before the SDK acknowledges them, serialize claimed turns by topic, and hold ambiguous runs for operator review instead of automatically repeating a provider call. Project-dispatched partner delegation uses a structured, allowlisted action with correlation, a timeout and loop limits; direct delegation follows the agent's lightweight task-ID convention.

The SQLite baseline is v11. Marked v9 and v10 databases are upgraded transactionally using additive schema changes; data from earlier or unknown schemas is not automatically migrated. Read-only dry-run policy snapshots can read a v9, v10 or v11 policy without changing the source.

Native backend memory remains available. A single configured, editable shared memory file is available for immediate saves and background consolidation. Agents are prompted to preserve provenance, but their native file operations are not validated by the project. It is not a per-group memory database. Each background consolidation starts a separate fresh backend session and holds uncertain outcomes for review.

## Consequences

- Feishu event scopes, application publication, membership and bot-to-bot behavior must be verified in the selected test group. Local fake-client tests do not prove live delivery or completeness.
- Project dry-run isolates project-owned state and dispatch, but cannot stop a native-permission agent from independently changing local or external state.
- A bounded local cache and an existing provider session are not a complete copy of all Feishu history. Reconnects mark cache gaps; on-demand backfill is subject to access and paging limits.
- The existing manual approval, operator, retention and health contracts continue to apply. A v9 upgrade and the new optional `cards` dependency for event reception require corresponding operations documentation.
