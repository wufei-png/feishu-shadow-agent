# Separate case baseline and evaluation run configs

Status: accepted

Captured and golden artifacts retain the actual `config.yaml` used when they were authored as the Case Baseline Config. Evaluation execution uses only the configuration explicitly provided through `--config` as the Evaluation Run Config. The two files are never merged, and the embedded case config is not an implicit override.

## Consequences

Every run copies its Evaluation Run Config into the run directory and reports both `case_config_hash` and `run_config_hash`, plus whether they differ. Config drift is allowed because testing a new backend, prompt, lifecycle, or policy configuration is a primary eval workflow. Exact baseline replay remains available by passing the case's embedded config to `--config`.

`owner.open_id` is the exception: it participates in normalization, sender-role classification, and mention detection, so it must match the Case Baseline Config. A mismatch is an artifact/configuration error rather than an experiment. Sensitive-field scanning applies independently whenever either case or run artifacts copy a config.
