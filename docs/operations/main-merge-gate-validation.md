# main 合并门禁验收记录

本记录只保存 GitHub 仓库规则和 CI 行为的脱敏证据；不保存 token、飞书内容或本地
配置。

## 已启用的规则

- Ruleset ID：`23727358`（[GitHub ruleset](https://github.com/wufei-png/feishu-shadow-agent/rules/23727358)）。
- 名称：`main pull-request CI gate`；target：`refs/heads/main`；enforcement：`active`。
- `pull_request` 规则要求经 PR 合入，但 `required_approving_review_count` 为 `0`，适合
  single-owner 仓库。
- `required_status_checks` 为严格模式，包含[运维说明](main-merge-gate.md)列出的九项
  checks；`Coverage (non-blocking)` 未列入。
- `bypass_actors` 为空，规则读取结果的 `current_user_can_bypass` 为 `never`；紧急路径
  只能依照运维说明中有记录的临时 enforcement 调整和恢复执行。

## 已完成的行为证据

- 2026-09-20，临时 Git commit object 尝试更新 `refs/heads/main` 被远端 `GH013` 拒绝；
  返回“Changes must be made through a pull request”和“9 of 9 required status checks are
  expected”。该对象未进入远端分支。

## 进行中的 PR 验收

本文件所在 PR 在全部 checks 未完成时先尝试 merge，以记录门禁拒绝；等待完整 CI 成功
后再由同一 PR 验证允许 merge。完成后在本节补充 PR 链接、拒绝结果、允许合入结果和
对应 CI run。
