# main 合并门禁

`main` 默认只接受通过 Pull Request 合入的变更。规则由 GitHub repository ruleset
管理；仅 GitHub users `wufei-png` 和 `wufei2` 是永久 bypass actors（`always`），可在
需要时绕过 PR 和必需检查。除此以外的 actor（包括其他 repository admin）必须走正常 PR
流程。单 owner 仓库不要求第二位审批人。

## 必需检查

以下是 CI workflow 当前实际产生、并且 ruleset 必须要求的 check 名。矩阵项按
完整显示名匹配；不得使用 workflow、job id 或前缀的猜测名称。

| 类别 | 必需 check |
| --- | --- |
| 静态质量 | `Backend static quality` |
| 后端矩阵 | `Backend tests (Python 3.11, UTC)` |
| 后端矩阵 | `Backend tests (Python 3.11, Asia/Shanghai)` |
| 后端矩阵 | `Backend tests (Python 3.11, America/Los_Angeles)` |
| 后端矩阵 | `Backend tests (Python 3.12, UTC)` |
| 后端矩阵 | `Backend tests (Python 3.13, UTC)` |
| 前端 | `Operator Console build` |
| 包 | `Build and inspect package` |
| Python 依赖 | `Python dependency audit` |

`Coverage (non-blocking)` 保持 non-blocking，不加入 required checks。新增、重命名
或删除 CI job 时，必须先在 PR 中更新本表和 ruleset，再合入 workflow 变更；否则
门禁可能意外放行或永久阻塞。

## 日常合入

1. 从最新 `main` 创建主题分支，提交并推送。
2. 创建以 `main` 为 base 的 PR，等待上表所有 checks 成功。
3. owner 自行审阅；本仓库不配置无法满足的第二审批人。
4. 使用 GitHub PR merge 完成合入，保留 PR、check 和 merge commit 作为审计记录。

除 `wufei-png` 和 `wufei2` 外，禁止通过管理员直推绕过该流程。其他 actor 的直推被
拒绝时，应创建或修正 PR。两个 bypass actor 直推 `main` 时，必须先在 GitHub issue 或
PR comment 中记录原因、授权 owner、目标 SHA、时间和对应 CI run；不得把 bypass 用作
常规合入路径。

## 紧急绕过与恢复

紧急情况可以临时关闭该 ruleset 的 enforcement，但只能由 owner 执行，并且必须
先在 GitHub issue 或 PR comment 中记录事件、原因、授权 owner、目标 SHA、开始时间
和预期恢复时间。记录不得包含飞书内容、token 或其他敏感数据。

1. 读取并记录当前 ruleset JSON 及 ID：

   ```bash
   gh api repos/wufei-png/feishu-shadow-agent/rulesets/$RULESET_ID
   ```

2. 在记录中附上临时调整理由后，禁用 enforcement：

   ```bash
   gh api --method PATCH repos/wufei-png/feishu-shadow-agent/rulesets/$RULESET_ID \
     -f enforcement=disabled
   ```

3. 仅推送已记录的紧急 SHA；立即恢复原 ruleset。不得新增或扩大既有 `wufei-png`、
   `wufei2` 以外的 bypass actor：

   ```bash
   gh api --method PATCH repos/wufei-png/feishu-shadow-agent/rulesets/$RULESET_ID \
     -f enforcement=active
   ```

4. 读取 ruleset，确认 `enforcement` 为 `active`、`bypass_actors` 仅包含
   `wufei-png` 和 `wufei2`、必需 check 与本表一致；随后记录恢复时间、紧急 SHA 和
   对应 CI run。若恢复失败，停止后续直推并在同一记录中标记为未恢复。

紧急提交仍须触发 CI。事后修复或补充测试通过普通 PR 合入；“API 调用成功”不等于
门禁已恢复或提交行为已验证。

## 验收记录

启用时在本节记录 ruleset ID、启用时间、测试 PR、直推拒绝结果、缺 check 的拒绝
结果、全部 check 后的合入结果，以及紧急绕过配置的读取结果。当前记录由会话
`post-mvp/02-main-protection` 在规则实际启用后补全。
