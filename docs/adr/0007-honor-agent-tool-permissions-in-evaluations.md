# Honor agent tool permissions in evaluations

Status: accepted

Router, task-session, full-chain, and judge calls honor the `tool_permissions` selected by the Evaluation Run Config, including `full_access`. Evaluation does not silently downgrade permissions, require a separate confirmation flag, limit repeat count, or create a disposable workspace.

## Consequences

Production backend behavior remains aligned with the selected run config, but a model with full tool access may modify files, execute commands, or reach external systems, and repeated trials may repeat those effects. The eval safety guarantee is narrower: Python orchestration does not run the Dispatcher or send Feishu replies and owner notifications. Reports record the effective tool-permission profile, and the CLI warns when full access is combined with repeated trials, but the warning does not block execution.
