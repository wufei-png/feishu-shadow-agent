# Product Policy Store owns runtime policy

Status: accepted

Feishu Shadow Agent will treat Product Policy Store as the runtime source of truth for global reply policy and per-chat policy. `config.yaml` remains useful as a Policy Import Source through explicit import and replace commands, but daemon processing and UI state read Product Policy from SQLite so operator changes are durable, auditable, and aligned with the future UI experience.

## Considered Options

- Keep YAML as the runtime source of truth: simpler now, but UI edits would require file writes, reload semantics, and awkward drift handling.
- Use YAML defaults plus DB overrides: flexible, but creates two live policy sources and makes effective policy harder to explain.
- Move runtime Product Policy into DB: slightly more upfront work, but gives the UI one product-grade policy boundary.

## Consequences

Daemon startup must fail closed when DB global policy is not initialized. Policy import is explicit; daemon startup must not silently synchronize or overwrite policy from `config.yaml`.
