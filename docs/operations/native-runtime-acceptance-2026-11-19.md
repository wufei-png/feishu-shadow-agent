# 原生运行时验证记录：2026-11-19

本记录区分本地模拟、真实飞书只读检查与实际发送。本次没有实际发送飞书消息；独立测试应用尚未接入，不能将真实飞书业务场景记为通过。

## 本地验证

- 原生入口按 `codex` / `hermes` 选择运行时；Hermes 分支不打开项目 SQLite，且拒绝把 Gateway 启动标为 dry-run。Codex 使用独立的项目发布队列，旧 daemon 入口保留原语义。
- 假飞书与合成事件覆盖群主区和话题路由、回执续接、开题后移交、owner 私聊、转发输入、关注及取消、子 Agent 回投、补充指令、人工接手、发送不确定后的处置、审批和旧策略兼容。这些结果证明本地逻辑，不证明飞书实际投递与权限。
- 隔离 `CODEX_HOME`、真实 `codex-cli 0.167.0` 和 App Server 验证新建与恢复会话、`excludeTurns`、线程卸载；真实模型轮经假飞书客户端生成 dry-run 回复预览。另以合成 MCP 和 shell 标记验证 dry-run 工具隔离；没有连接飞书。
- 同一版本的真实 Codex 临时线程将版本更正判断为当前任务、将另一个任务判断为无关；该临时线程没有生成 rollout 文件。判断过程使用隔离环境，没有调用业务工具或连接飞书。
- 早期版本曾在隔离 App Server 验证生产禁网和工作目录约束；本次需求审查后移除这些强制限制，生产轮次沿用原生工具配置。此前隔离结果仅适用于旧配置。
- 从新建开始关闭 Codex 用户级记忆的线程，恢复后仍关闭；已加载且原先开启记忆的旧线程不能仅靠恢复参数证明关闭。群、私聊笔记使用不同文件，但同一系统用户仍可读取两者。
- 旧 SQLite 策略缺少 `memory_maintenance` 时保留原默认值；新配置默认关闭。v9、v10、v11 的升级及在途数据保留由本地测试覆盖。

提交 `7d675c1` 时，全量 `uv run --locked pytest -q` 为 1195 通过、1 跳过、2 条依赖弃用警告。最终代码提交 `c6eff38` 的全量结果为 **1265 通过、1 跳过、2 条依赖弃用警告**；跳过的是需要实际发送审批卡片的飞书端到端测试。`ruff check .`、`ruff format --check .`、`uv run --locked --extra cards pyright`、`pre-commit run --all-files` 和 `git diff --check` 通过。

独立审查对照了原始需求会话与实现。审查发现的原生工具配置、任务补充和跨话题来源、子 Agent 状态、发布时序等问题已修复并增加回归覆盖；对最终代码的复审没有新的 P0–P3 发现。跨话题引用链的特殊事件组合只做了假客户端验证，真实飞书客户端是否产生该组合尚未确认。

## Hermes 主机迁移

- 原 `default` 和 `wechatbot` 是两个 v0.20.0 systemd 用户服务，均保存相同飞书应用凭据；v0.23.2 原生迁移预检将其识别为阻断项。
- 数据和原 systemd 单元已备份至 `~/.hermes/backups/native-runtimes-20261119-ifp4wnga/`，原 v0.20.0 安装保留。为保证本次验证不发飞书消息，两个旧 profile 的飞书应用 ID 和密钥已从运行配置中暂时移除，备份中保留原值。
- 已安装 v0.23.2，原生迁移器将 `wechatbot` 并入 `default` 主网关并移除独立服务。`hermes gateway list` 确认两个 profile 由同一主进程承载；最新 `systemctl --user show` 显示主网关 `active/running`。
- 新建 `coworker` profile 使用占位群和用户 ID，飞书平台关闭。无凭据状态下通过项目启动入口确认它由主网关承载，随后重新停放；配置独立测试应用前保持停放。
- 本次需求审查使用 v0.23.2 的 Feishu adapter 和 Gateway 授权代码及隔离假事件，验证指定群其他成员 @ 可进入、未 @ 静默、其他群拒绝、owner 私聊允许、其他人私聊拒绝；owner 仅有 `open_id` 和同时有 `user_id` 两种情况均覆盖。未连接飞书，也未改变现有 profile。

## 真实飞书只读检查

- 本机默认 CLI workspace 曾在本次工作中通过用户身份验证，但最新状态为 `needs_refresh`；Hermes 应用对应的 CLI workspace 中 bot 身份可用，user token 缺失。
- 以 Hermes 应用的 bot 身份读取原配置群的信息成功，返回普通群类型。这不是独立测试应用的验收。
- `lark-cli im +messages-reply --dry-run` 返回请求预览，没有调用发送接口。隔离临时数据库的 `doctor --mode dry-run` 中，Codex App Server、多 Agent、bot 身份及应用 ID 匹配检查通过；整体未通过，原因是 Hermes workspace 的 user token 和 scopes 无法验证，磁盘空闲比例触发告警。
- 原项目配置所指生产 SQLite 为 v8；新代码只提供 v9、v10、v11 到 v12 的迁移，未修改原库。不要直接用该生产配置启动新版本。

## 尚待验收

需要独立飞书测试应用及其测试群、应用内的 owner ID 和必要授权，才能验证长连接入站、无 @ 事件、真实话题位置及回执、转发正文、Hermes Gateway 回投和人工接手续跑。当前没有这些真实联调证据。

生产线程沿用 Codex 的原生网络、MCP 和 sandbox 设置；项目提供的只读群工具限定当前授权群，此前已有的 UUID 可能未注册这些工具。需要完成独立飞书应用联调，才能验收实际消息入站和回投。
