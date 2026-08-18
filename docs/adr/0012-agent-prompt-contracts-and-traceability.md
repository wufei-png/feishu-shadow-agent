# Agent prompt contracts and traceability

Status: Accepted

## Context

Runtime prompts need to serve several backends without making provider-specific CLI behavior part of the product contract. The task session also has a structured output boundary, optional local Context Access, and evaluation prompts that must remain separate from production behavior. Full prompt text is useful for debugging but can contain conversation content and should not be the normal audit payload.

## Decision

1. Pydantic models in `agent_output_contract.py` are the canonical agent-output contract. Python validation is authoritative; provider-native schemas and the compact Task Session contract are derived aids.
2. Backend-neutral runtime builders stay in `prompt.py`; shared evidence, data-boundary, skill, and Context Access rules stay in `prompt_instructions.py`; output-enum escalation stays in each kind's output contract or kind-specific instruction; provider adapter instructions stay in Codex/Hermes/Claude adapters; evaluation judge prompts stay under `evals/`.
3. The prompt catalog identifies each runtime/evaluation prompt kind and version. The exact backend-neutral business prompt is hashed with UTF-8 SHA-256. Production audit rows store kind, version, and hash without storing full prompt text by default; eval artifacts store versions alongside existing hashes.
4. Context Access is a logically read-only, bounded interface. In `full_access`, its read-only behavior intentionally relies on the model following the prompt instructions; this is not a physical sandbox. Feishu external writes remain code-owned and are protected independently by policy, approval, dry-run, idempotency, and dispatch gates.

## Consequences

- Changing business prompt text or its output contract requires updating the prompt catalog version and focused prompt tests.
- Changing only provider wrapper instructions does not change the business prompt hash; provider/runtime metadata and git state remain separate evidence.
- Operators can correlate an audit/eval result with the exact business prompt without exposing full conversation content in normal audit views.
- Deployments needing physical local read-only isolation must select `read_only` or add an external sandbox; `full_access` is not a security boundary for local tools.
