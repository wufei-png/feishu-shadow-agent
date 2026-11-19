# Current Architecture Boundaries

This document describes the current implementation boundary for Feishu Shadow Agent. It is the first document to read when extending the runtime, operator surface, or agent prompt contracts. Older files under `docs/plans/` record phased implementation history and should not override this current boundary.

## Product Shape

Feishu Shadow Agent is a local, single-owner Feishu assistant with three main runtime paths:

| Path | Feishu and state ownership | Agent execution |
| --- | --- | --- |
| Shadow `daemon` | The project polls user messages and owns tasks, policy, approvals, dispatch and SQLite state. | Hermes, Codex or Claude Code handles routed tasks. |
| Codex native coworker | The project receives bot events for a configured group and owner private chat, owns SQLite state, and publishes replies. | Codex App Server owns sessions and native subagents. |
| Hermes Gateway coworker | Hermes Gateway owns the Feishu connection, sessions, replies and profile state. The project launcher does not load its SQLite or configuration. | Hermes runs the `coworker` profile and native delegation. |

The `daemon` command also supports the existing project-managed coworker path, where `send_mode` selects agent-direct or project dispatch. Each project daemon selects shadow or coworker at startup. Separate long connections for the same Feishu application share event delivery, so stop the previous receiver before switching.

Current non-goals remain deliberate:

- No multi-owner, multi-tenant, or remote web console.
- No project-provided LaunchAgent, cron, systemd service, desktop binary, or service installer. Hermes manages its own Gateway service.
- No runtime merge between project `config.yaml` and Product Policy Store.
- No automatic resend by project dispatch when its send state is uncertain.
- No direct Feishu sending workflow in shadow mode; its prompts use project dispatch, while native tool capabilities depend on the backend environment.
- No WebSocket message ingestion for shadow tasks. Shadow uses user-authenticated polling; the SDK connection handles approval cards and, in project coworker mode, message events for the configured group and supported private chats.
- No automatic Product Policy changes learned from approval feedback.

## Document Precedence

Code and tests establish implemented behavior. This document, the mode-specific operating guides and `docs/configuration.md` describe the current interface; `CONTEXT.md` defines project terms. ADRs and specifications carry the scope and status stated in each file. Phase plans and dated acceptance records preserve decisions and evidence from their time. Start with the [documentation index](../README.md).

## Code-Owned Rules

These rules belong in deterministic code and tests. Do not delegate them to prompt wording alone:

- Feishu identity selection: shadow ingestion uses user reads, user P2P replies, bot owner notifications, bot-preferred group replies, and bot resource downloads. Project-owned coworker reads and replies use the configured bot identity without a user-identity fallback; the agent's own tools use their effective permissions and are guided by prompt instructions.
- Message ingress eligibility: shadow group `@owner` and P2P rules remain separate from the opt-in coworker group's bot events, trigger and peer-correlation rules. Both suppress unintended `@All` triggers and distinguish self messages.
- Shadow routing ownership and shortcuts: a revision stays with its original active task; otherwise reply-to, thread, and burst-window attachment are evaluated in that order before TaskRouter. Each daemon runs exactly one business mode until restart: shadow ingestion and retries are stopped in coworker mode, and project-owned dispatch and approval queues are limited to the active mode. Coworker sessions are group-shared for ordinary messages and topic-shared for threads; Codex native private requests have separate sessions that referenced replies can resume. Every structural lookup remains scoped to the current chat.
- Project Product Policy resolution: Product Policy Store as runtime truth, explicit Policy Import Source comparison, and fail-closed behavior when global policy is missing.
- Project-managed Codex coworker execution uses one persistent App Server: root turns are serialized within a group/topic session and can run concurrently across topics. The project records child state and resumes the root on completion; an accepted steer is not proof of a completed action. The legacy `daemon` coworker path calls Hermes once per turn. Native Hermes Gateway manages its own sessions and background delegation.
- SQLite schema: empty project stores initialize to v13; marked v9, v10, v11 and v12 stores upgrade transactionally. Other versions or application markers are rejected. Read-only policy snapshots can read marked v9, v10, v11 and current v13 stores without modifying the source.
- Removing a group policy holds its watching tasks, queued processing retries and pending send actions. Re-adding the policy does not release these holds; an operator explicitly reopens a task or retries an action. In-flight provider calls cannot be retracted, and successful send evidence remains authoritative.
- Resource gates: bot joined, resource download enabled, size/quota checks, retryable download failures, and owner notification on blocked resources.
- Storage capacity guard: `runtime_storage_capacity` measures the sqlite, log, and resource roots and blocks ingestion and real sends only when free space on the tightest runtime filesystem drops below the fixed critical floor. A low-space warning never blocks runtime work, and a measurement failure never becomes a blocking verdict.
- Bot membership facts: active probes and attributed bot send/download failures may derive present, absent, or unknown runtime state; they never mutate owner-authored Product Policy or auto-join a chat.
- Reply gates: answerability/decision-reason combination validation, direct group mention requirement, empty reply rejection, forbidden mention cleanup, and identity fallback rules.
- Dispatch safety: validated same-chat reply destination, dry-run before send, idempotency key reuse for the same destination, single active send constraint, chat/thread readback verification, owner notification for failed/uncertain or unverified reply sends, and manual recovery for uncertain sends.
- Ingestion completeness: malformed or empty CLI output and incomplete pagination fail the fixed window without advancing its checkpoint; only known CLI warning prefixes may be removed before parsing JSON.
- Processing recovery: only terminal or externally blocked `task_router`, `resource_download`, and `task_session` stages may be manually queued. Each retry has a fresh claim token, remains bound to the source revision and task ownership, and never turns uncertain dispatch into an automatic resend.
- Operator mutations: all state-changing owner actions, including card callbacks, go through Operator Command services and return `CommandResult`.
- Operator read models: CLI status and console reads go through `OperatorQueryService`, not direct store DTO snapshots.
- Approval provenance: dry-run approvals/actions never become production sends; production requires a fresh production approval.
- Full-chain retention: after the configured content window, messages, inactive task state, task background content/reasons, approvals, actions, dispatch results, resources, agent audits, approval commands/feedback, processing errors, and log payloads are scrubbed in place. Minimal audit rows remain. Automatic daemon retention also protects pending work and its related records; manual `retention prune` retains its existing behavior and can scrub paused work.

## Agent Prompt Architecture And Trust Boundary

Runtime prompt responsibilities are split by authority:

- `agent_output_contract.py` owns the shadow mode's canonical Pydantic output models and runtime validation. Provider-native output schemas and the compact Task Session contract are derived from those models. Coworker project dispatch has its own structured output; agent-direct mode accepts natural output and does not dispatch that output again.
- `prompt.py` owns backend-neutral runtime prompt builders and task-specific instructions. `prompt_instructions.py` owns shared evidence, data-boundary, skill, and Context Access rules. Output-enum escalation stays in each kind's output contract or kind-specific instruction.
- Codex/Hermes/Claude adapter instructions remain provider-specific and are injected by the adapter layer. Evaluation judge prompts remain under `evals/` and are not added to production prompts.
- The runtime prompt catalog includes `router`, `task_session`, `reply_postprocess`, `owner_style_refresh`, `coworker`, and `coworker_memory`; production audits include the coworker prompt kinds, while owner-style refresh has no production audit row. Evaluation adds `ingress_judge`, `semantic_judge`, and the generic `structured_output` fallback. `agent_audits.request_type` is the persisted prompt kind. Audit rows and eval artifacts record the catalog version and SHA-256 of the exact backend-neutral business prompt; full prompt text remains debug-only.

Context Access provides a read-only URI, allowed tables and query scope; it does not isolate other backend tools. The project's built-in Feishu dispatch applies policy, approval, dry-run and idempotency checks. In shadow and legacy coworker paths, native tool calls use the backend's effective permissions even during dry-run. Codex native coworker uses a restricted tool configuration in dry-run; its production calls inherit Codex settings. Hermes Gateway uses Hermes permissions and has no project-level dry-run.

Owner-provided task background is a separate, explicit evidence channel. Operator Command appends a task-scoped version for set, replace, or clear; it does not reset `agent_session_id` or change task ownership. `TaskSessionRunner` reads only the latest non-cleared value and injects it only when building a fresh provider session. Changes made after a session is created apply only to a future fresh session; resumed sessions never receive them. The prompt labels background as owner-supplied evidence rather than an instruction. It asks the model to weigh the source, context and timing of conflicts and to request owner review when a material conflict remains unresolved. This is model judgment, not a deterministic conflict detector.

## Agent-Owned Judgement

These shadow-mode decisions can be handled by the agent inside code-provided candidates and schemas. Coworker agent-direct mode instead uses native tools as described above:

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
- `membership.py`: active bot-membership probes, fact expiry, failure attribution, and episode-level owner notification deduplication. Unknown and expired facts must not be treated as confirmed absence.
- `processing.py`: task-level orchestration from route result to task session, postprocess, reply gate, approval, or send action. New feature branches should prefer extracting helpers over adding more nested branches here.
- `ingestion.py`: ingress orchestration, resource acquisition, and claimed manual processing retries. A task-router retry must recover the original deterministic placeholder rather than treating the later failure audit as a final route.
- `dispatcher.py`: dispatch claiming, dry-run, actual send, readback, stale sending detection, and manual recovery.
- `coworker.py` and `coworker_store.py`: project-managed coworker events, sessions and the legacy daemon path.
- `coworker_native.py`, `coworker_native_store.py` and `coworker_native_state.py`: Codex native coworker routing, delivery, watches and handoff state.
- `codex_app_server.py`: the Codex App Server connection, native sessions and dynamic read tools.
- `approval_cards.py`: deterministic Card JSON construction. Cards bind one concrete approval and expose only the supported resolution actions.
- `card_actions.py`: owner-only `card.action.trigger` parsing, event-id idempotency, atomic command/feedback application, callback connection health, and daemon wake-up.
- `operator_queries/`: read-only operator DTOs. It may derive status, overdue fields, feedback metrics, health issues, and recommended actions, but it must not mutate state.
- `operator_commands.py`: explicit operator mutations, including auditable processing and dispatch recovery. Console and CLI commands should call this facade instead of reaching into store transactions directly.
- `store/sqlite_store.py`: SQLite persistence and transactional primitives. Avoid adding new product-facing read models here.
- `storage_usage.py`: read-only storage measurement shared by operator reads and health checks. It only stats and walks configured paths, and never creates, truncates, or rewrites them.
- `prompt.py` and `context_access.py`: agent input contracts. Every model-visible field must have a current decision purpose.
- `console_api.py`: local HTTP adapter only. Keep business decisions in query/command services.

## Approval Decisions And Feedback

The task-session output keeps `answerability` as the send decision and uses `decision_reason` for audit and feedback slices. Valid combinations are intentionally asymmetric:

- `auto_reply`: `decision_reason` may be `null`; if present it must be `sufficient_evidence_low_risk`.
- `no_reply`: requires `no_response_needed`, `already_resolved`, or `duplicate_or_stale`.
- `needs_owner`: requires `insufficient_evidence`, `commitment_or_authorization`, `sensitive_or_high_impact`, `write_or_permission`, or `human_judgment_required`.

A `needs_owner` output may omit a reply draft. It becomes a pending approval that cannot be approved without a draft; the owner can supply a final reply through the existing send command, subject to destination validation.

Owner resolution writes exactly one immutable feedback outcome: `suggestion_sent`, `edited_sent`, `no_send_keep_watching`, or `no_send_end_task`. A fixed optional feedback reason and short note may accompany the outcome. Metrics and UI can aggregate these facts, but runtime policy must not mutate itself from them.

Interactive approval cards are optional. Outbound cards use `lark-cli`; in shadow mode the SDK long connection receives `card.action.trigger`. In project-managed coworker mode the same connection also receives message events. Each card callback is scoped to its approval and owner, uses the Feishu event ID as an idempotency key, and applies the operator command and feedback atomically. Text commands remain available when the card connection is unavailable.

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
