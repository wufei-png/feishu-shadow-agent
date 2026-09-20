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

## 2026-09-21 合并转发资源 capability preflight

- 本机 `.venv/node_modules/.bin/lark-cli` 仍为 `1.0.56`；user 与 bot 凭据均已完成服务端验证。
- user 对唯一受控 chat 的降序读取成功，返回 8 条消息，未找到 `merge_forward` 容器。
- bot 对同一列举调用返回 API `230002`。没有 current container ID 可供 image/file 资源下载测试，因此没有执行下载调用、写入 runtime store 或保存文件。
- 这不是 container-ID 能力失败的证据，也不推翻现有的 fail-closed 占位降级。需要在获准测试 chat 提供同时带 image 和 file 的当前合并转发容器，并以 container ID 由 bot 分别下载两种资源后，才能决定是否实施接入。

## 运行库与执行界限

原 `data/agent.sqlite3` 的 `PRAGMA user_version` 为 `1`，当前 runtime schema 为 `7`，
且迁移路径不接受 v1。经 owner 明确授权放弃历史后，旧库已归档在 ignored `data/` 目录，
配置使用新的 v7 runtime store。新库已执行 `policy import-config`；一条 global policy 和
一条 chat policy 回读为 `matches`，`doctor --config config.yaml` 的 13 项 critical 与 4 项
warning 均通过。

所有现场运行均为 `daemon --dry-run`，未传 `--send-owner-notifications`，未发送真实回复。
每个 daemon 运行期间只有它一个 SQLite 写者；运行状态只通过 JSONL 读取，SQLite 的脱敏
检查均在该 daemon 正常停止后进行。

## 验收状态

| 链路 | 状态 | 缺少的现场证据 |
| --- | --- | --- |
| S4 有界 ingest 追赶 | 通过 | 已在获准测试群以 1,001 条 `group_at_me` 消息验证固定窗口的 20 页 / 1,000 条 cap、处理 cursor、跨 tick 与干净重启恢复、终端 checkpoint 推进及全量追赶。 |
| S5 membership 恢复 | 部分通过 | 已得到 `present → absent → unknown（过期）→ recovered` 的真实 daemon/Effective Policy 时间线和 episode 通知预览；未以真实资源或真实回复发送来验证降级，因为本会话明确保持 dry-run。 |

## 2026-09-20 现场结果

- 测试群由 owner 创建并授权；测试策略在 Product Policy Store 中以 `auto_reply=false`、
  `bot_preferred` 和 user fallback 写入。该一次 owner 操作将 Policy Audit 基线设为 3。
- 使用 bot identity 发送了 1,001 条带稳定 idempotency key 的直接 mention 测试消息；分页
  回读为 21 页、1,001 条。30 次初始发送失败经过只重试缺失 id 后补齐，没有重复计数。
- 三次 dry-run daemon 读取该窗口时，`group_at_me` 分别在第 6、3、1 页以 `command failed`
  失败。结构化 envelope 后续确认这些调用均为受控外层中断：`exit_code=-2`、
  `timed_out=false`，且没有 stderr 或 JSON 错误；不是 CLI、认证/scope、限流、page token
  或 ingest 处理错误。
- membership 主动探测先记录 `present`。owner 移除 bot、确认 TTL 到期后再次探测记录
  `absent`，Effective Policy 的 `bot_joined=false`；停止刷新超过 TTL 后只读 Effective Policy
  得到 `unknown` 且回退为 `bot_joined=true`；owner 重新加入 bot 后探测记录 `present` 并创建
  recovery notification。全过程 Policy Audit 保持 3。
- 所有 daemon 运行均为 dry-run，未传 `--send-owner-notifications`；notification 仅预览，
  `send_reply` 的 production count 为 0。

## S4 修复与复验（通过）

### 根因与最小修复

- 实际代码缺陷不是上述外层中断：旧路径的 tick deadline 只限制了获取，逐页启动
  `lark-cli` 可以耗尽 30 秒预算，随后已获取批次的 normalize/routing 没有可恢复的处理边界。
- `7bf19db` 将 deadline 覆盖到逐消息处理，在边界保存当前批次起始 page token、已完成数量
  和不含原文的 replay-prefix digest；不会把 checkpoint 越过未处理尾部。
- `b8d48b3` 在同一预算内保留最多 5 秒给处理；`2c17fbf` 将 digest 收紧为语义字段，避免
  易变的搜索 enrichment 让安全重放错误重置。
- `aa2ec03` 在剩余原 page cap 内使用一次 `--page-all --page-limit` 请求，返回的聚合页数仍
  计入原 20 页 / 1,000 条 cap；它减少相同 cap 内的 CLI 子进程开销，不提高 cap 或预算。
  真实 capability probe 在 18.82 秒返回 1,000 条、仍带后续 token。
- `ef4a205` 在处理耗尽预算时保留原获取原因。因此 backlog 同时记录
  `reason=tick_budget_exhausted` 与 `fetch_reason=page_cap_exhausted`，不会掩盖 cap 事实。

### 脱敏现场时间线（UTC）

- `16:05:47`，同一固定窗口首次以 20 页 / 1,000 条触发 deferred backlog；处理 cursor 为
  22。随后 cursor 依次推进至 74、129、169，checkpoint 没有推进。
- 在 `16:10:48` 的完整 tick 后正常停止并重启。重启后 cursor 从 169 继续至 204、232，
  JSONL 没有 `ingestion_processing_cursor_reset`；重启段新增 63 个路由审计记录恰好对应
  cursor 的 63 项前进，未重放已完成前缀。
- 后续同一单写者追赶持续将 cursor 推进至 984。`16:44:49` 首批处理完成，backlog 为
  `page_cap_exhausted`、存在后续 token、没有 processing cursor，且 checkpoint 仍未推进。
- `16:45:39` 终端页（一页、一条）完成；`last_success_at` 推进到固定窗口的
  `2026-09-20T15:20:51+00:00`，group checkpoint 不再有 backlog。
- 停止 daemon 后，受控 marker 消息为 `1,001 / 1,001`，缺失为 0；routing audit 的不同
  `message_id` 同为 1,001。总 audit 行数包含此前运行的审计轨迹，不能当作唯一消息数。
  从 `16:05:00+00:00` 至终端完成没有新的 `message_page_fetch_failed`。

`last_drain` 的 `651 pages / 32,501 messages` 是 processing cursor 重放时的累计获取尝试，
不是单 tick cap 或唯一源消息数；每个发生 cap 的 tick 仍严格为 20 页 / 1,000 条。所有停机
均在 `retention_skipped`（dry-run tick 完成）后执行，未将受控中断写成 CLI 故障。
