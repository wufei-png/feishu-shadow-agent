# Separate Evaluation Scenarios from labels

Status: accepted

Golden evaluation cases store execution inputs in `eval_case.yaml` and expected results in `labels.yaml`. Message ids, ingestion sources, task fixtures, mode, and setup order define an Evaluation Scenario; expected router routes, task aliases, answerability, watch actions, and reference answers are labels. Keeping these concerns separate prevents runners from treating ground truth as setup configuration and lets promotion validate executable inputs independently from scoring expectations.

## Consequences

Captured review templates may combine scenario inputs and proposed expectations for convenient human editing, but `promote` must validate and split them into a self-contained `eval_case.yaml` and minimal `labels.yaml`. No additional `scenario.yaml` is introduced.

Router, task-session, and full-chain cases keep all available raw messages in one deduplicated `messages.jsonl`. Message roles come only from scenario references; golden promotion copies only referenced messages, so no duplicate `raw_seed.json` or role-wrapped context file is needed.
