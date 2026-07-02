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

**Action Queue**:
The prioritized operator-facing set of items that need attention, such as pending approvals, overdue approvals, failed dispatch actions, stale sending actions, and blocking health or policy initialization issues.
_Avoid_: Metrics dashboard, raw status list, activity feed

**Operator Query**:
A read-only operator-facing view of current state. It may derive overdue or recommended-action fields, but it must not mutate state.
_Avoid_: Status mutation, maintenance action

**Message Detail**:
A read-only operator view of one Feishu message's processing context, including related task links, routing decisions, approvals, dispatch actions, and recorded outcomes. It explains what happened around a message without generating new dispatch previews or mutating state.
_Avoid_: Replay command, raw message dump, dispatch preview generation

**Operator Command**:
An explicit owner action that may mutate state, such as approving a reply, recovering a dispatch action, expiring approvals, or updating Product Policy.
_Avoid_: Store helper, UI callback

**Settings Catalog**:
A stable product field map that defines which settings the Operator Console exposes, how they are grouped, whether they are editable, and which source owns them. It is not a dynamic schema engine and should not mirror every raw config field.
_Avoid_: Raw config schema, dynamic form engine, every setting is editable
