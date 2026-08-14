# Current Architecture Boundaries

This document describes the current implementation boundary for Feishu Shadow Agent. It is the first document to read when extending the runtime, operator surface, or agent prompt contracts. Older files under `docs/plans/` record phased implementation history and should not override this current boundary.

## Product Shape

Feishu Shadow Agent is a local, single-owner Feishu assistant. It watches direct owner mentions in group chats and P2P messages, asks an agent backend for semantic work, and replies only through code-owned policy, approval, dispatch, and audit gates.

Current non-goals remain deliberate:

- No multi-owner, multi-tenant, or remote web console.
- No LaunchAgent, cron, systemd, desktop binary, or service installer.
- No runtime merge between `config.yaml` and Product Policy Store.
- No automatic resend when dispatch state is uncertain.
- No agent-side execution of Feishu sends.
- No full WebSocket message ingestion. User-authenticated polling remains the message source; the optional official SDK connection handles card actions only.
- No automatic Product Policy changes learned from approval feedback.

## Document Precedence

Use this order when documents disagree:

1. `docs/architecture/current-boundaries.md`
2. `CONTEXT.md` for product language
3. `docs/adr/*.md` for accepted architectural decisions
4. `docs/specs/*.md` for current product and API contracts
5. `docs/plans/*.md` for historical phased implementation context

Plans can still be useful for rationale, acceptance commands, and old edge cases, but they are not the live contract once the corresponding phase has landed.

## Code-Owned Rules

These rules belong in deterministic code and tests. Do not delegate them to prompt wording alone:

- Feishu identity selection: user reads, user P2P replies, bot owner notifications, bot-preferred group replies, and bot resource downloads.
- Message ingress eligibility: group `@owner`, P2P, `@All` suppression, sender role classification, and loop guard.
- Deterministic routing shortcuts: reply-to, thread, and burst-window attachment.
- Product Policy resolution: Product Policy Store as runtime truth, explicit Policy Import Source comparison, and fail-closed behavior when global policy is missing.
- Resource gates: bot joined, resource download enabled, size/quota checks, retryable download failures, and owner notification on blocked resources.
- Reply gates: answerability/decision-reason combination validation, direct group mention requirement, empty reply rejection, forbidden mention cleanup, and identity fallback rules.
- Dispatch safety: dry-run before send, idempotency key reuse, single active send constraint, readback verification, owner notification for failed/uncertain reply sends, and manual recovery for uncertain sends.
- Operator mutations: all state-changing owner actions, including card callbacks, go through Operator Command services and return `CommandResult`.
- Operator read models: CLI status and console reads go through `OperatorQueryService`, not direct store DTO snapshots.
- Approval provenance: dry-run approvals/actions never become production sends; production requires a fresh production approval.
- Full-chain retention: after the configured content window, messages, inactive task state, approvals, actions, dispatch results, resources, agent audits, approval commands/feedback, processing errors, and log payloads are scrubbed in place. Minimal audit rows remain; only a watching task whose `watch_until` is still in the future delays scrubbing.

## Agent-Owned Judgement

These decisions can be handled by the agent, but only inside code-provided candidates and schemas:

- Ambiguous task ownership after deterministic routing fails.
- Whether the task has enough evidence for `auto_reply`, `needs_owner`, or `no_reply`.
- Selecting a decision reason valid for that answerability: `needs_owner` and `no_reply` require one, while `auto_reply` may omit it or use only `sufficient_evidence_low_risk`.
- Drafting the plain reply text before `SendComposer` applies Feishu-safe mention handling.
- Choosing `reply_target_message_id` from code-provided candidates such as current message and root message.
- Choosing `watch_action` for keeping a task open or closing it.
- Rewriting expression during reply postprocess while preserving meaning, facts, uncertainty, commitments, and action items.

If an agent output crosses these bounds, the code should reject it, downgrade to owner review, or record an audit instead of silently accepting it.

## Module Ownership

- `routing.py`: deterministic routing, owner takeover, duplicate route recovery, and the boundary that decides whether Hermes TaskRouter is needed.
- `policy.py`: Product Policy resolution for resource and reply decisions. Keep chat policy fallback rules here instead of copying them into processing, UI, or store code.
- `processing.py`: task-level orchestration from route result to task session, postprocess, reply gate, approval, or send action. New feature branches should prefer extracting helpers over adding more nested branches here.
- `dispatcher.py`: dispatch claiming, dry-run, actual send, readback, stale sending detection, and manual recovery.
- `approval_cards.py`: deterministic Card JSON construction. Cards bind one concrete approval and expose only the supported resolution actions.
- `card_actions.py`: owner-only `card.action.trigger` parsing, event-id idempotency, atomic command/feedback application, callback connection health, and daemon wake-up.
- `operator_queries/`: read-only operator DTOs. It may derive status, overdue fields, feedback metrics, health issues, and recommended actions, but it must not mutate state.
- `operator_commands.py`: explicit operator mutations. Console and CLI commands should call this facade instead of reaching into store transactions directly.
- `store/sqlite_store.py`: SQLite persistence and transactional primitives. Avoid adding new product-facing read models here.
- `prompt.py` and `context_access.py`: agent input contracts. Every model-visible field must have a current decision purpose.
- `console_api.py`: local HTTP adapter only. Keep business decisions in query/command services.

## Approval Decisions And Feedback

The task-session output keeps `answerability` as the send decision and uses `decision_reason` for audit and feedback slices. Valid combinations are intentionally asymmetric:

- `auto_reply`: `decision_reason` may be `null`; if present it must be `sufficient_evidence_low_risk`.
- `no_reply`: requires `no_response_needed`, `already_resolved`, or `duplicate_or_stale`.
- `needs_owner`: requires `insufficient_evidence`, `commitment_or_authorization`, `sensitive_or_high_impact`, `write_or_permission`, or `human_judgment_required`.

Owner resolution writes exactly one immutable feedback outcome: `suggestion_sent`, `edited_sent`, `no_send_keep_watching`, or `no_send_end_task`. A fixed optional feedback reason and short note may accompany the outcome. Metrics and UI can aggregate these facts, but runtime policy must not mutate itself from them.

Interactive approval cards are optional. Outbound cards still use `lark-cli`; the official Python SDK long connection receives only `card.action.trigger`. Each callback is scoped to its approval, validates the owner open ID, uses the Feishu event ID as the command idempotency key, applies the operator command and feedback atomically, returns a queued acknowledgement, and wakes the daemon. Text commands remain available regardless of connection health.

## Agent Input Field Checklist

Before adding, keeping, or renaming a field in an agent-facing prompt or context access payload, answer all five questions in the implementation or review notes:

1. Purpose: Which exact agent decision or output can this field change?
2. Producer: Which code path produces it, and is it derived from trusted runtime state?
3. Consumer: Which instruction or output schema makes the agent use it?
4. Failure path: What happens if the field is absent, invalid, stale, or ignored?
5. Regression test: Which focused test asserts the field shape or proves metadata-only fields stay out?

Default rule: remove metadata-only fields from model input. Keep operational metadata in audit records, logs, DTOs, or test fixtures unless the agent must use it to make the current decision.

Prompt and context changes should normally update focused tests in `tests/test_prompt.py` or a similarly narrow test module. Broad behavior tests are not a substitute for prompt-shape regression coverage.

## Store And Read Model Boundary

The store owns persistence and transaction safety. The operator surface owns product-facing reads.

Allowed store responsibilities:

- Current-schema bootstrap and connection setup.
- Transactional inserts, updates, claims, and recovery primitives.
- Small data access helpers used by routing, processing, dispatch, and maintenance services.

Disallowed store responsibilities:

- New console DTOs.
- New CLI status snapshots.
- Product-level recommended actions.
- Query-only health issue composition.

Use `OperatorQueryService.dashboard_snapshot()` for status output and `/api/dashboard`. Use focused `OperatorQueryService` methods for detail pages. If a new view needs a new read shape, add it to the query boundary and keep the store helper narrow.

## When To Add Documents

Use `CONTEXT.md` only for project language. Do not put implementation details there.

Use an ADR only when a decision is hard to reverse, surprising without context, and the result of a real trade-off.

Use `docs/architecture/` for current implementation boundaries and extension rules. Keep historical phase plans under `docs/plans/` as records, not live architecture contracts.
