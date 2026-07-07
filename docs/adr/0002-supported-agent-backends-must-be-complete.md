# Supported agent backends must be complete

Status: accepted

Feishu Shadow Agent will treat a backend provider as formally supported only when one selected provider can cover the full Backend Capability Set: task routing, task sessions, reply postprocess, and owner style refresh. This keeps policy, approval, dispatch, audit, health checks, and operator debugging tied to one accountable runtime instead of mixing partial providers across one Feishu task.

## Considered Options

- Expose partial provider support per capability: flexible, but it fragments session ownership, health status, audit records, and failure diagnosis.
- Allow different providers for router, task sessions, and postprocess: useful for experiments, but too complex for the first public configuration surface.
- Require one selected provider to implement the complete capability set: stricter upfront, but gives operators one backend to validate and one failure boundary to reason about.

## Consequences

Codex and Claude Code support should be added through provider-specific adapters behind the existing Agent Backend boundary. Configuration should keep one selected `agent_backend.provider` plus provider-specific config sections, and runtime health should check only the selected provider for executable/auth/structured-output readiness.

Each adapter must map the existing permission and context-isolation semantics to that provider's CLI model. Adapters should use provider-native structured output enforcement instead of relying only on prompt wording, while prompts may still include schemas as model guidance.

Task sessions are provider-owned. Tasks should persist the backend provider associated with the stored session id so a later provider switch cannot accidentally resume a session created by another runtime. The daemon should stay fail-closed when the selected provider cannot pass its health and structured-output checks.

Backend tool permissions should have only two formal modes: `read_only` and `full_access`. The earlier `guarded_write` mode is not a reliable non-interactive safety boundary and should be removed from the active configuration surface rather than migrated, because the project has not shipped with stable user data depending on it. Read-only context access should be modeled as read-only context access, not as a side effect of enabling local write tools.
