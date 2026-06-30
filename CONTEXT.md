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

**Operator Query**:
A read-only operator-facing view of current state. It may derive overdue or recommended-action fields, but it must not mutate state.
_Avoid_: Status mutation, maintenance action

**Operator Command**:
An explicit owner action that may mutate state, such as approving a reply, recovering a dispatch action, expiring approvals, or updating Product Policy.
_Avoid_: Store helper, UI callback
