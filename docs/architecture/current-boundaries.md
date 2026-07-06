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
- Reply gates: answerability gate, direct group mention requirement, empty reply rejection, forbidden mention cleanup, and identity fallback rules.
- Dispatch safety: dry-run before send, idempotency key reuse, single active send constraint, readback verification, and manual recovery for uncertain sends.
- Operator mutations: all state-changing owner actions go through Operator Command services and return `CommandResult`.
- Operator read models: CLI status and console reads go through `OperatorQueryService`, not direct store DTO snapshots.

## Agent-Owned Judgement

These decisions can be handled by the agent, but only inside code-provided candidates and schemas:

- Ambiguous task ownership after deterministic routing fails.
- Whether the task has enough evidence for `auto_reply`, `needs_owner`, or `no_reply`.
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
- `operator_query.py`: read-only operator DTOs. It may derive status, overdue fields, health issues, and recommended actions, but it must not mutate state.
- `operator_commands.py`: explicit operator mutations. Console and CLI commands should call this facade instead of reaching into store transactions directly.
- `store/sqlite_store.py`: SQLite persistence and transactional primitives. Avoid adding new product-facing read models here.
- `prompt.py` and `context_access.py`: agent input contracts. Every model-visible field must have a current decision purpose.
- `console_api.py`: local HTTP adapter only. Keep business decisions in query/command services.

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

- Schema migration and connection setup.
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
