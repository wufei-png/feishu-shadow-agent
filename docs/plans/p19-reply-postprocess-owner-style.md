# P19 Reply Postprocess Owner Style Plan

## Summary

P19 adds an optional outbound reply postprocess step so agent-generated Feishu
replies can better match the owner's natural reply habits and avoid common AI
writing patterns.

The feature has two independent guidance sources:

```text
owner_style profile generated from recent owner replies
humanizer_zh skill guidance
```

Either source can be enabled alone, or both can be enabled together. Runtime
must call the model at most once for reply postprocess.

## Background

The current task session decides whether a message is answerable, what to reply,
which message to reply to, and whether the task keeps watching or closes. That
task session is a stateful Hermes session and can be resumed for follow-up task
messages.

Reply postprocess has a narrower responsibility: rewrite only the expression of
an already generated candidate reply. It must not choose a reply target, change
answerability, add facts, add commitments, add times, add conclusions, or add
action items.

Postprocess should therefore be a new one-shot Hermes call, not a resumed task
session.

Implementers in a fresh context should read:

- `CONTEXT.md`
- `docs/specs/feishu-shadow-agent-mvp-design.md`
- `docs/specs/feishu-shadow-agent-flows.md`
- `src/feishu_shadow_agent/processing.py`
- `src/feishu_shadow_agent/task_session_runner.py`
- `src/feishu_shadow_agent/hermes.py`
- `src/feishu_shadow_agent/feishu/lark_cli.py`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/operator_query.py`
- `src/feishu_shadow_agent/health.py`
- `frontend/operator-console/src/screens/ApprovalsScreen.tsx`
- `frontend/operator-console/src/screens/DispatchScreen.tsx`

## Confirmed Decisions

- Postprocess applies only to agent candidate replies.
- Owner-written `/send <task_id> <final reply>` remains untouched and is sent
  exactly as written.
- Postprocess uses a new one-shot Hermes session with read-only tool access.
- Postprocess does not resume the task session.
- Postprocess runs before `SendComposer` and before reply policy gates.
- Both `auto_reply` and `needs_owner` candidate replies run through postprocess
  when postprocess is enabled.
- `no_reply` outputs do not run postprocess.
- The postprocess prompt stays short and direct. It may tell the model to read
  the configured owner style profile path and humanizer skill path.
- Do not add path-level allowlist logic in v1. Use read-only tool access and
  explicit prompt instructions.
- Do not add Feishu mention warnings to the prompt. Existing `SendComposer`
  remains the final Feishu safety and mention composition layer.

## Non-goals

- No new reply-target model step.
- No task-session prompt rewrite for owner style.
- No automatic daemon refresh of owner style profile.
- No SDK migration.
- No Feishu card UI.
- No path-level sandbox/allowlist for postprocess in v1.
- No configurable length threshold.
- No postprocess of owner-written `/send` text.

## Configuration

Add a new root config section:

```yaml
reply_postprocess:
  enabled: false
  max_turns: 4
  model: null
  provider: null
  owner_style:
    enabled: false
    profile_path: data/owner_style.zh.md
    refresh:
      lookback_days: 30
      max_samples: 300
      min_samples: 20
  humanizer_zh:
    enabled: false
    skill_path: /Users/wufei2/.agents/skills/humanizer-zh/SKILL.md
```

Config semantics:

- Pydantic defaults must keep `reply_postprocess.enabled: false` so existing
  configs preserve current behavior.
- `config.example.yaml` should show the disabled default and include comments or
  nearby guidance for the enablement steps.
- `reply_postprocess.enabled: false` disables all checks and all postprocess
  calls.
- If `reply_postprocess.enabled: true`, at least one child source must be
  enabled. If both child sources are disabled, config validation should fail.
- If `owner_style.enabled: false`, runtime does not check `profile_path`.
- If `owner_style.enabled: true` and `profile_path` is missing or unreadable,
  runtime logs an error and sends the candidate to owner review.
- If `humanizer_zh.enabled: false`, runtime does not check `skill_path`.
- If `humanizer_zh.enabled: true` and `skill_path` is missing or unreadable,
  runtime logs an error and sends the candidate to owner review.
- `model` and `provider` default to the main Hermes config when null.
- `max_turns` is separate from task-session max turns and defaults to `4`.

## Owner Style Refresh

Add an explicit command such as:

```bash
python -m feishu_shadow_agent reply-style refresh --config config.yaml
```

The refresh command:

- Uses `lark-cli im +messages-search` with `--sender <owner.open_id>`,
  `--start`, `--end`, `--page-all`, and `--no-reactions`.
- Searches all owner replies visible through user identity, not only configured
  chats.
- Applies only light Python filtering before summarization:
  - empty text
  - `/approve`, `/reject`, `/send`, and similar operator commands
  - link-only or resource-placeholder-only messages
  - single messages above an internal sample character cap
- Lets the summarizer decide which remaining samples are useful.
- Fails without writing if fewer than `min_samples` remain.
- Uses the same `reply_postprocess.model/provider` settings as runtime
  postprocess.
- Writes a Markdown profile only after summarization succeeds.
- Writes through a temp file and atomically replaces the target profile.
- Leaves the previous profile untouched on any failure.

Add:

```bash
python -m feishu_shadow_agent reply-style refresh --dry-run
```

Dry run behavior:

- Pulls and filters samples.
- Prints sample counts and basic stats.
- Does not call Hermes.
- Does not write or replace the profile file.

## Owner Style Profile Format

Use Markdown, not YAML, for the generated profile. The profile is a local runtime
artifact and should live under ignored `data/` by default.

Suggested shape:

```markdown
# Owner Reply Style Profile

generated_at: 2026-07-05T00:00:00+08:00
lookback_days: 30
sample_count: 300

## Style Summary

...

## Common Patterns

- ...

## Avoid

- ...

## Examples

### Quick confirmation

Owner-like reply:
...

### Delay or decline

Owner-like reply:
...

### Suggest next step

Owner-like reply:
...
```

Do not retain raw message ids, chat ids, names, links, phone numbers, or full
private conversation context. Keep at most three short scenario examples. These
examples are owner-like reply examples, not question-answer pairs.

## Runtime Flow

For task-session outputs with `answerability` of `auto_reply` or
`needs_owner`:

```text
task session output
-> validate reply_target_message_id
-> reply postprocess when enabled
-> SendComposer cleanup and group mention composition
-> reply policy gate
-> send action or approval
```

Postprocess output schema:

```json
{
  "status": "ok",
  "final_reply": "..."
}
```

Allowed statuses:

```text
ok
needs_owner
```

Runtime validation:

- Only `status: ok` uses `final_reply`.
- `final_reply` must be non-empty.
- Invalid JSON, schema failure, empty text, model failure, timeout, unreadable
  configured guidance file, or `status: needs_owner` sends the candidate to
  owner review.
- Keep a code-only disaster guard:
  - `len(final_reply) > len(original_reply) * 3` and `len(final_reply) > 300`
  - or `len(final_reply) > 2000`
- The disaster guard reason is `postprocess_length_growth`.
- Do not expose these length thresholds in config.

Postprocess prompt requirements:

- If owner style is enabled, ask the model to read the configured owner style
  profile path and align with it.
- If humanizer is enabled, ask the model to read the configured skill path and
  avoid AI writing patterns.
- If only one source is enabled, mention only that source.
- Tell the model to rewrite expression only.
- Tell the model not to add facts, commitments, times, conclusions, or action
  items.
- Require strict JSON output with `status` and `final_reply`.

## Success Behavior

When postprocess succeeds:

- Use the postprocessed text for approval preview and action payload text.
- `/approve` sends the postprocessed text.
- Store the original candidate and postprocess metadata in the approval/action
  payload.
- `last_agent_reply` should reflect the actual sent text after `SendComposer`,
  not the original candidate.

Suggested payload metadata:

```json
{
  "postprocess": {
    "applied": true,
    "status": "ok",
    "enabled_guidance": ["owner_style", "humanizer_zh"],
    "original_reply": "...",
    "final_reply": "...",
    "owner_style_profile_path": "data/owner_style.zh.md",
    "humanizer_skill_path": "/Users/wufei2/.agents/skills/humanizer-zh/SKILL.md"
  }
}
```

## Failure Behavior

When postprocess fails or returns `needs_owner`:

- Do not auto-send.
- Keep the task `watching`.
- Create a send-reply approval.
- Approval preview shows the original candidate reply.
- `/approve <approval_id>` sends the original candidate after existing
  `SendComposer` cleanup.
- `/send <task_id> <final reply>` sends owner-written final text unchanged.
- `/reject <approval_id>` cancels this send candidate and keeps the task
  watching.
- Normal non-postprocess approvals keep their existing reject behavior.

Use a small payload signal for the special reject behavior:

```json
{
  "keep_watching_on_reject": true,
  "postprocess": {
    "applied": false,
    "status": "failed",
    "failure_reason": "profile_missing",
    "fallback": "original_candidate"
  }
}
```

Do not introduce a broad reject behavior enum in v1.

Store behavior:

- `apply_approval_command` must read the approval payload before the reject
  branch closes the task.
- When `keep_watching_on_reject` is true, reject only the current approval and
  cancel pending actions linked to that approval if any exist.
- Do not call the normal close-after-reject path for this special approval.
- Do not close the task.
- Do not expire, reject, or cancel other pending approvals for the same task.
- Leave the task in `watching`.

## Agent Backend

Extend `AgentBackend` with semantic methods:

```python
def reply_postprocess(prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
    ...

def owner_style_refresh(prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
    ...
```

`HermesCliClient` should still use `hermes chat -q -Q`, but these methods own:

- one-shot behavior
- `reply_postprocess.max_turns`
- read-only tool policy
- `reply_postprocess.model/provider` overrides

Implementation requirements:

- `reply_postprocess` and `owner_style_refresh` must use safe/read-only Hermes
  toolsets even when the global daemon `tool_permissions` is `full_access`.
- These methods must not pass `--resume`.
- These methods must not inject task-session skills.
- `task_session` remains the only method that resumes task sessions and includes
  task-session skills.

Use the existing `AgentInvoker` retry mechanism. Do not add separate retry
configuration for postprocess or refresh.

## Auditing

Record each runtime postprocess call through `agent_audits`:

```text
request_type = reply_postprocess
```

Record:

- backend provider
- task id
- input message ids
- response JSON
- error
- latency
- tool permission profile as read-only
- prompt only when `debug.save_full_agent_io` is true

Style refresh may also record an audit entry or structured log event, but it
must not persist raw owner samples unless debug behavior is explicitly designed
later.

## Doctor And Health

Add doctor checks guarded by config:

- If postprocess is disabled, do not check profile or skill paths.
- If owner style is enabled, check profile path exists and is readable.
- If humanizer is enabled, check skill path exists and is readable.

These checks should be warnings, not critical failures. The daemon can continue
running because each affected reply will fail into `needs_owner`.

Do not add a doctor warning for `reply_postprocess.enabled: true` with both child
sources disabled. That is a config validation error and should be caught before
`HealthSuite.run()` proceeds.

## Operator Console

Do not add a new Postprocess page in v1. Surface status where operators already
inspect approvals and dispatch actions.

Approvals:

- List row badge for `postprocess_failed` or `postprocess_needs_owner`.
- Detail panel section showing:
  - postprocess status
  - enabled guidance sources
  - failure reason
  - approve behavior, such as `original_candidate`
  - whether reject keeps the task watching

Dispatch:

- Detail payload should show `postprocess.applied`, enabled guidance sources,
  and original/final reply metadata for sent or pending actions.

Message Detail and Task Detail:

- Do not duplicate full postprocess metadata.
- Existing related approval/action links are enough.

## Files To Update

- `src/feishu_shadow_agent/config.py`
- `schemas/config.schema.json`
- `config.example.yaml`
- `src/feishu_shadow_agent/hermes.py`
- `src/feishu_shadow_agent/agent_backend.py`
- `src/feishu_shadow_agent/processing.py`
- `src/feishu_shadow_agent/prompt.py` or a new postprocess prompt module
- `src/feishu_shadow_agent/cli.py`
- `src/feishu_shadow_agent/feishu/lark_cli.py`
- `src/feishu_shadow_agent/store/sqlite_store.py`
- `src/feishu_shadow_agent/health.py`
- `src/feishu_shadow_agent/operator_query.py`
- `frontend/operator-console/src/screens/ApprovalsScreen.tsx`
- `frontend/operator-console/src/screens/DispatchScreen.tsx`
- `frontend/operator-console/src/types.ts`
- `docs/configuration.md`
- `docs/testing.md`
- `tests/test_operator_commands.py` or store-level approval command tests

## Test Plan

Config:

- Postprocess disabled ignores missing owner profile and humanizer skill.
- Pydantic defaults keep postprocess disabled.
- Postprocess enabled with both child sources disabled fails config validation.
- Enabled owner profile missing yields runtime needs-owner and doctor warning.
- Enabled humanizer skill missing yields runtime needs-owner and doctor warning.
- `model/provider` null inherits main Hermes settings.
- `max_turns` affects only postprocess and refresh calls.

Runtime:

- `auto_reply` candidate runs postprocess before `SendComposer` and gate.
- `needs_owner` candidate also runs postprocess before approval creation.
- `no_reply` does not run postprocess.
- Postprocess success uses final text in approval/action payload.
- Postprocess success keeps original candidate in metadata.
- Postprocess failure creates approval with original candidate preview.
- Postprocess failure does not close the task.
- Rejecting a postprocess-failure approval keeps the task watching.
- Rejecting a postprocess-failure approval does not expire, reject, cancel, or
  close other pending approvals for the same task.
- Rejecting ordinary approvals keeps existing behavior.
- `/send` owner final reply is not postprocessed.
- Length disaster guard routes to owner review.
- `last_agent_reply` records actual sent text.

Hermes backend:

- `reply_postprocess` uses `--toolsets safe` even when the client was created
  with global `full_access`.
- `owner_style_refresh` uses `--toolsets safe` even when the client was created
  with global `full_access`.
- Neither method emits `--resume`.
- Neither method emits `--skills`.
- `task_session` still emits `--resume` when a session id exists and still
  includes configured task-session skills.

Refresh:

- `reply-style refresh --dry-run` does not call Hermes or write files.
- Refresh pulls owner messages with sender and time filters.
- Light filtering removes commands, empty text, link-only/resource-only samples,
  and oversized samples.
- Fewer than `min_samples` fails without replacing old profile.
- Successful refresh writes Markdown through temp file then atomic replace.
- Failed Hermes refresh leaves old profile untouched.

Audit and UI:

- Runtime postprocess records `agent_audits` with request type
  `reply_postprocess`.
- Full prompt is stored only when debug full agent IO is enabled.
- Approval DTO exposes postprocess metadata needed by Console.
- Dispatch detail exposes postprocess metadata.
- Console badges and detail fields render postprocess failure and success
  states without requiring a new page.

## Acceptance

```bash
.venv/bin/python -m pytest -q tests/test_config.py
.venv/bin/python -m pytest -q tests/test_hermes_cli.py
.venv/bin/python -m pytest -q tests/test_lark_cli.py
.venv/bin/python -m pytest -q tests/test_p3_hermes_approval.py
.venv/bin/python -m pytest -q tests/test_processing_collaborators.py
.venv/bin/python -m pytest -q tests/test_operator_query.py
.venv/bin/python -m pytest -q tests/test_console_api.py
npm --prefix frontend/operator-console run build
.venv/bin/python -m pytest -q
git diff --check
```

## Handoff Notes

- Keep postprocess as a text rewrite function, not another task reasoning step.
- Keep owner style refresh explicit and operator initiated.
- Keep profile content local under ignored `data/` by default.
- Do not silently fall back when an enabled guidance file is unreadable.
- Do not make length thresholds configurable in v1.
- Do not postprocess owner-written final replies.
