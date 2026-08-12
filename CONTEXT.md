# Feishu Shadow Agent Context

Feishu Shadow Agent is a local operator-owned assistant for Feishu conversations. This glossary keeps product and operator-surface language precise as the backend grows toward a UI.

## Language

**Operator Surface**:
The local product surface used by the owner to inspect daemon state, review approvals, recover dispatch actions, and manage runtime policy.
_Avoid_: UI API, admin panel, status dump

**Product Policy**:
Runtime business policy that controls whether and how the assistant may reply, download resources, or fall back between Feishu identities.
_Avoid_: Config, YAML setting, feature flag

**Product Policy Store**:
The durable source of truth for Product Policy during runtime.
_Avoid_: Chat config, YAML policy

**Policy Import Source**:
The policy fields in `config.yaml` used by an explicit import command to seed or replace Product Policy Store records.
_Avoid_: Runtime policy, live config

**Effective Policy**:
The Product Policy that applies to a specific chat or message after combining global policy, per-chat policy, and fallback rules.
_Avoid_: Raw config, policy row

**Policy Audit**:
A durable record of a Product Policy change, including old value, new value, actor, reason, and time.
_Avoid_: Log line, debug output

**Policy Import Diff**:
A read-only comparison between a Policy Import Source and the Product Policy Store. It explains what an explicit import or replace would change; it is not runtime drift because the import source is not live policy.
_Avoid_: Config drift, runtime drift

**Approval Blocker**:
A pending approval that blocks an automated send while leaving the task lifecycle unchanged.
_Avoid_: waiting approval status, task status

**Owner Notification**:
A bot private message to the owner that carries the minimum context and commands needed to resolve an Approval Blocker without opening the Operator Surface.
_Avoid_: alert stub, raw dispatch action, Console-only pointer

**Owner Escalation**:
The system ending automated ownership of a task and notifying the owner that manual handling is required. It requests a handoff; unlike Human Takeover, it does not claim that the owner has already acted.
_Avoid_: Human Takeover, blocked task, failed task

**Action Queue**:
The prioritized operator-facing set of items that need attention, such as pending approvals, overdue approvals, failed dispatch actions, stale sending actions, and blocking health or policy initialization issues.
_Avoid_: Metrics dashboard, raw status list, activity feed

**Operator Query**:
A read-only operator-facing view of current state. It may derive overdue or recommended-action fields, but it must not mutate state.
_Avoid_: Status mutation, maintenance action

**Operator Query Slice**:
A focused read-only module that owns one operator-facing read model, such as Message Detail or Health Issues, while a thin compatibility facade exposes it to CLI and local API routes.
_Avoid_: Raw SQL helper, screen component, store method

**Message Detail**:
A read-only operator view of one Feishu message's processing context, including related task links, routing decisions, approvals, dispatch actions, and recorded outcomes. It explains what happened around a message without generating new dispatch previews or mutating state.
_Avoid_: Replay command, raw message dump, dispatch preview generation

**Operator Command**:
An explicit owner action that may mutate state, such as approving a reply, recovering a dispatch action, expiring approvals, or updating Product Policy.
_Avoid_: Store helper, UI callback

**Agent Input Contract**:
The product boundary that defines what information an agent turn can see and which decision or output each field is allowed to influence.
_Avoid_: Prompt dump, debug metadata, everything we know

**Reply Context**:
The minimal Task Session input that identifies the current and root messages, allowed reply targets, and optional chat type without carrying operational counts or conversation text.
_Avoid_: Metadata block, task snapshot, audit context

**Output Contract**:
The compact model-visible description of the final Task Session fields and their cross-field rules; provider-native schemas and Python validation remain the enforcement boundary.
_Avoid_: Full prompt schema, draft response, intermediate status

**Message Acquisition**:
The retrieval of raw Feishu messages through source-specific Lark searches and chat or thread windows, including active-watch task and watch-key matching. It determines which raw messages and acquisition sources are available for local evaluation, not whether they belong to a task.
_Avoid_: Message Eligibility, routing, ingest decision

**Message Eligibility**:
A source-aware deterministic decision made after normalization and before task routing that determines whether an acquired message may enter task ownership and handling. It receives acquisition sources but never reads task/store state, and it must preserve meaningful owner interventions without choosing a target task.
_Avoid_: Message Acquisition, router outcome, handed to process_raw_message

**Temporary Eval Store**:
An isolated file-backed SQLite store rebuilt for one evaluation case from explicit scenario inputs. It carries the production state model during a run but is never copied from or written back to the production runtime store.
_Avoid_: Production snapshot, copied runtime store, in-memory fixture, eval truth source

**Evaluation Scenario**:
The explicit messages, ingestion sources, task fixtures, mode, and target that define how one evaluation case is executed. It contains inputs only; expected routes, answerability, and reference answers belong to labels.
_Avoid_: Golden label, captured context window, production snapshot

**Evaluation Clock**:
The deterministic business time of an evaluation turn, derived from the current scenario message's `sent_at`. Wall-clock run timestamps describe execution only and must not change model input, lifecycle state, or scoring.
_Avoid_: Run creation time, replay time, explicit evaluation_at override

**Evaluation Task Alias**:
A case-local stable name such as `task_1` used to compare task identity without depending on production or temporary database IDs. Router scenarios declare aliases on task fixtures; full-chain runs assign them to setup-created tasks in creation order.
_Avoid_: Production task ID, temporary row ID, task title

**Evaluation Trial**:
One isolated execution of an evaluation case. Repeated model evaluations rebuild the Temporary Eval Store and provider session for every trial so one result cannot affect another.
_Avoid_: Retry, resumed run, repeated judge call

**Trial Evidence Bundle**:
The retained report, event log, and optional full prompts for one Evaluation Trial. Rebuildable SQLite state and resource copies are deleted after evidence has been materialized.
_Avoid_: Temporary Eval Store, production snapshot, full runtime backup

**Expected Skill Set**:
The human-reviewed Task Session label listing which skills an Agent Backend should load for an Evaluation Scenario. It is evaluation truth and never part of the Agent Input Contract.
_Avoid_: Explicit skill injection, production skill config, prompt hint

**Discoverable Skill**:
A skill whose identity and description are available to an Agent Backend for optional selection. Discoverability does not guarantee that the skill is activated for a Task Session.
_Avoid_: Preloaded skill, active skill, Expected Skill Set

**Native Skill Request**:
The provider-specific configuration that requests a skill for a Task Session. Hermes uses configured paths with its native CLI; Codex uses a preinstalled skill name. A request is not evidence that the runtime loaded the skill.
_Avoid_: Explicit Skill Activation, loaded skill, Expected Skill Set

**Explicit Context Path**:
An absolute path shown in the initial Task Session prompt for a non-native skill that the Agent may read when needed. It is explicitly marked as not loaded and is not a Native Skill Request.
_Avoid_: Native skill, loaded skill, installed skill

**Evaluation Skill Trace**:
The sanitized Trial Evidence that separates requested skills from skills proven loaded by runtime-native session evidence, and records which catalog repositories were read. It is diagnostic evidence and never part of production prompts, replies, or audit responses.
_Avoid_: Context trace, model self-report, production audit trace

**Evaluation Resource Fixture**:
A successfully captured Feishu file or image referenced by an Evaluation Scenario. Its bytes and checksum are copied into each trial so production resource preflight and prompt construction can run without Feishu network access.
_Avoid_: Live download fallback, production resource cache, resource failure label

**Evaluation Task Fixture**:
The minimal explicit task state used to rebuild Router candidates in a Temporary Eval Store: a stable alias, production task status, task label, and ordered message membership. Derived chat, watch-key, count, and lifecycle timestamps are not duplicated in the fixture.
_Avoid_: Production task row, copied task ID, full database snapshot

**Case Baseline Config**:
The immutable `config.yaml` copied into a captured or golden case to identify and reproduce the configuration under which that artifact was authored. It is not silently merged into later runs.
_Avoid_: Active run config, config defaults, golden policy

**Evaluation Run Config**:
The `config.yaml` explicitly selected by `--config` for one evaluation run. It is the only configuration used for backend, model, tool permissions, prompt, lifecycle, and policy behavior, and a copy is stored with that run.
_Avoid_: Case Baseline Config, merged config, implicit override

**Agent Backend**:
The selected coding-agent runtime that interprets Feishu task context and returns schema-bound decisions or reply candidates for the assistant.
_Avoid_: LLM, model, Hermes-only path

**Backend Capability Set**:
The complete set of agent-owned work a runtime must perform to be product-grade for this assistant: task ownership routing, task handling, reply expression rewriting, and owner style summarization.
_Avoid_: CLI feature list, partial integration

**Supported Agent Backend**:
An Agent Backend that is allowed in runtime configuration because it satisfies the Backend Capability Set under the assistant's policy, approval, dispatch, and audit boundaries.
_Avoid_: Experimental provider, best-effort backend, partially supported backend

**Settings Catalog**:
A stable product field map that defines which settings the Operator Console exposes, how they are grouped, whether they are editable, and which source owns them. It is not a dynamic schema engine and should not mirror every raw config field.
_Avoid_: Raw config schema, dynamic form engine, every setting is editable
