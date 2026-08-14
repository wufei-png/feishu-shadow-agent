# Retain trial evidence, not rebuildable state

Status: accepted

Evaluation runs retain a Trial Evidence Bundle containing the structured report and event log for each trial. Full prompts are retained only when the Evaluation Run Config enables `debug.save_full_agent_io`. Temporary Eval Stores and trial-local resource copies are deleted after evidence is written because they can be rebuilt from immutable scenario artifacts.

## Consequences

Every trial starts from a newly initialized current-schema SQLite file in a unique private temporary directory, seeds messages and task fixtures in explicit scenario order, and uses the Evaluation Clock for all business-state writes. A per-case lock exposes only the current directory through a stable `.trial-slots/<case-hash>/current` alias, so SQLite URIs, resource paths, prompt hashes, temporary row IDs, and insertion order are deterministic for a fixed scenario. Concurrent runs of the same case serialize. Cleanup removes the alias and private directory after both success and failure; abandoned directories from a process crash are never reused.

Reports must therefore preserve the evidence needed for diagnosis: Router candidates and decisions, task alias mappings, task-session plans, raw model JSON, raw and effective replies, state-transition summaries, and would-send traces. This guarantees isolation and repeatability of eval-owned database state, not bit-for-bit model output or idempotence of external tool effects when the selected Agent Backend has `full_access`.
