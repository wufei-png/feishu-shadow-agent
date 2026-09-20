# ingest 与 membership 运行时现场验收记录

本记录只保存脱敏的环境条件、配置 hash、计数和错误类别；不保存飞书消息、chat ID、
owner/bot 身份、token、SQLite 内容或日志 payload。

## 2026-09-20 预检

- 时间：`2026-09-20T20:46:31+08:00`。
- 代码：`83c1542e7ebab68b12eaa38e17bc0a04e7574186`。
- 配置：`config.yaml` 的 SHA-256 为
  `9b36317085cae9640ed299b5a62befdc784e91dcf1bc0cea245cfaf3c154578e`；配置中有一个
  chat policy，但没有可公开记录的受控高流量源或获准 membership 测试 chat 标识。
- 已配置预算：tick `30s`、search `20 pages / 1000 messages`、active watch
  `5 pages / 250 messages`、overlap `120s`、membership TTL `300s`、unknown retry
  `60s`。
- `lark-cli` 路径可执行，版本为 `1.0.56`。

在临时空 SQLite 库中导入同一配置的 Product Policy 后，执行不含 `--send-test` 的
`HealthSuite`：13 项 critical 和 4 项 warning 均为 `ok`。这包括 lark-cli 认证验证、
必需 user scopes、bot identity、Codex 登录与能力检查；owner notification 仅走 CLI
dry-run。临时库在进程结束时删除，未写入项目的 `data/`、`logs/` 或飞书消息。

同一组 ingest / runtime membership 回归测试通过：`122 passed`（
`test_p2_ingestion_routing.py`、`test_policy_runtime.py`、`test_membership.py`、
`test_dispatcher.py`、`test_operator_query.py`）。该结果只证明 fixture 契约，不替代
下面的现场证据。

## 当前阻塞条件

原 `data/agent.sqlite3` 的 `PRAGMA user_version` 为 `1`，而当前 runtime schema 为
`7`。当前迁移路径只接受 v2、v3、v5 或 v6 的升级。经 owner 明确授权放弃该库的历史后，
它已移动到同一 ignored `data/` 目录的带日期归档名；配置路径现在使用新建的 v7 库。
新库已执行 `policy import-config`，有一条 global policy 和一个 chat policy，import
source 回读为 `matches`；正式 `doctor --config config.yaml` 的所有 13 项 critical 和 4
项 warning 均通过。

新库当前没有 ingest source、backlog、active task、pending action 或 membership fact。
以 user identity 对现有配置 chat 做了一次只读 `chat.members bots` 探测：调用成功，列表
中有一个 bot，但不是当前认证的 bot identity。该 chat 尚未被明确指定为可离群/重入的
测试 chat，因此没有把探测写为 runtime fact、没有改 Product Policy、没有创建 owner
notification，也没有发送消息。

当前 CLI 的 daemon 是持续运行模式；它没有计划中示例的 `--once` 参数。因此尚未启动
持续 daemon dry-run，也没有收集实际 ingest checkpoint。

## 验收状态

| 链路 | 状态 | 缺少的现场证据 |
| --- | --- | --- |
| S4 有界 ingest 追赶 | 失败，待诊断 | 获准测试群的 1,001 条 `group_at_me` 测试消息已分页确认（21 页），但 daemon 分别在第 6、3、1 页收到未分类的 `command failed`；未到 page/message cap，故没有可验证的 backlog/checkpoint 追赶或重启去重证据。 |
| S5 membership 恢复 | 部分通过 | 已得到 `present → absent → unknown（过期）→ recovered` 的真实 daemon/Effective Policy 时间线和 episode 通知预览；未以真实资源或真实回复发送来验证降级，因为本会话明确保持 dry-run。 |

## 2026-09-20 现场结果

- 测试群由 owner 创建并授权；测试策略在 Product Policy Store 中以 `auto_reply=false`、
  `bot_preferred` 和 user fallback 写入。该一次 owner 操作将 Policy Audit 基线设为 3。
- 使用 bot identity 发送了 1,001 条带稳定 idempotency key 的直接 mention 测试消息；分页
  回读为 21 页、1,001 条。30 次初始发送失败经过只重试缺失 id 后补齐，没有重复计数。
- 三次 dry-run daemon 读取该窗口时，`group_at_me` 分别在第 6、3、1 页以 `command failed`
  失败。该错误发生在 bounded drain 完成前，不能推断为 page cap、message cap 或 tick budget
  耗尽；必须先保留/分类 lark-cli 失败 envelope，再重跑固定窗口。
- membership 主动探测先记录 `present`。owner 移除 bot、确认 TTL 到期后再次探测记录
  `absent`，Effective Policy 的 `bot_joined=false`；停止刷新超过 TTL 后只读 Effective Policy
  得到 `unknown` 且回退为 `bot_joined=true`；owner 重新加入 bot 后探测记录 `present` 并创建
  recovery notification。全过程 Policy Audit 保持 3。
- 所有 daemon 运行均为 dry-run，未传 `--send-owner-notifications`；notification 仅预览，
  `send_reply` 的 production count 为 0。

后续现场验收前，owner 需要提供或确认：可恢复的测试 chat 及临时移除/重新加入 bot 的
授权，以及受控高流量测试源。满足这些条件后，使用真实 daemon tick、status、checkpoint
和脱敏日志计数补全本记录；不得把本次预检、fixture 测试或未授权 chat 探测标作现场通过。
