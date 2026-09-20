# main 合并门禁验收记录

本记录只保存 GitHub 仓库规则和 CI 行为的脱敏证据；不保存 token、飞书内容或本地
配置。

## 已启用的规则

- Ruleset ID：`23727358`（[GitHub ruleset](https://github.com/wufei-png/feishu-shadow-agent/rules/23727358)）。
- 名称：`main pull-request CI gate`；target：`refs/heads/main`；enforcement：`active`。
- 启用时间：2026-09-20T18:54:14.992+08:00（GitHub ruleset `created_at`）。
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

## PR 验收

- 测试 PR：[\#27](https://github.com/wufei-png/feishu-shadow-agent/pull/27)。
- 在该 PR 的 checks 仍为 `IN_PROGRESS` 时运行 `gh pr merge 27 --merge` 被拒绝，CLI
  返回“the base branch policy prohibits the merge”，PR 状态为 `BLOCKED`。未使用
  `--admin` 或 `--auto` 绕过门禁。
- 首轮 push 与 pull-request CI 分别为
  [35506441568](https://github.com/wufei-png/feishu-shadow-agent/actions/runs/35506441568) 和
  [35506443733](https://github.com/wufei-png/feishu-shadow-agent/actions/runs/35506443733)，
  九项 required checks 和 non-blocking Coverage 都成功。
- 本次最终验收记录的提交再次触发同一 PR 的 required checks；在它们成功后，使用普通
  merge 合入 #27。该 merge commit、PR checks 与本记录共同证明完整 checks 可以合并。
