# 原生运行时验证记录：2026-11-19

本记录区分本地模拟、真实飞书只读检查与实际发送。本次没有实际发送飞书消息；独立测试应用尚未接入，不能将真实飞书业务场景记为通过。

## 本地验证

- 原生入口按 `codex` / `hermes` 选择运行时；Hermes 分支不打开项目 SQLite，且拒绝把 Gateway 启动标为 dry-run。Codex 使用独立的项目发布队列，旧 daemon 入口保留原语义。
- 假飞书与合成事件覆盖群主区和话题路由、回执续接、开题后移交、owner 私聊、转发输入、关注及取消、子 Agent 回投、补充指令、人工接手、发送不确定后的处置、审批和旧策略兼容。这些结果证明本地逻辑，不证明飞书实际投递与权限。
- 隔离 `CODEX_HOME`、真实 `codex-cli 0.167.0` 和 App Server 验证新建与恢复会话、`excludeTurns`、线程卸载；真实模型轮经假飞书客户端生成 dry-run 回复预览。另以合成 MCP 和 shell 标记验证 dry-run 工具隔离；没有连接飞书。
- 使用相同的隔离 App Server 验证生产工具配置：新建线程、恢复线程和原生子 Agent 均能调用假飞书的群内只读话题工具；后台整理轮能调用假群历史并写入独立记忆目录。主 Agent 和子 Agent 的本地 shell TCP 连接被拒，监听器未收到连接。独立审查者对临时 Unix socket 的调用也得到拒绝。工作目录可写范围、`/tmp` 和项目状态分离均有本地检查；这些检查不构成真实飞书联调。
- 从新建开始关闭 Codex 用户级记忆的线程，恢复后仍关闭；已加载且原先开启记忆的旧线程不能仅靠恢复参数证明关闭。群、私聊笔记使用不同文件，但同一系统用户仍可读取两者。
- 旧 SQLite 策略缺少 `memory_maintenance` 时保留原默认值；新配置默认关闭。v9、v10、v11 的升级及在途数据保留由本地测试覆盖。

最终全量 `uv run --locked pytest -q`：1195 通过、1 跳过、2 条依赖弃用警告。按源码与 HEAD 静态对比，本次新增 61 个测试函数；参数化后的运行实例数不同。`ruff check .`、`ruff format --check .`、`pyright`、`pre-commit run --all-files` 和 `git diff --check` 通过。独立复审在修复后结论为 No findings。

## Hermes 主机迁移

- 原 `default` 和 `wechatbot` 是两个 v0.20.0 systemd 用户服务，均保存相同飞书应用凭据；v0.23.2 原生迁移预检将其识别为阻断项。
- 数据和原 systemd 单元已备份至 `~/.hermes/backups/native-runtimes-20261119-ifp4wnga/`，原 v0.20.0 安装保留。为保证本次验证不发飞书消息，两个旧 profile 的飞书应用 ID 和密钥已从运行配置中暂时移除，备份中保留原值。
- 已安装 v0.23.2，原生迁移器将 `wechatbot` 并入 `default` 主网关并移除独立服务。`hermes gateway list` 确认两个 profile 由同一主进程承载；最新 `systemctl --user show` 显示主网关 `active/running`。
- 新建 `coworker` profile 使用占位群和用户 ID，飞书平台关闭。无凭据状态下通过项目启动入口确认它由主网关承载，随后重新停放；配置独立测试应用前保持停放。

## 真实飞书只读检查

- 本机默认 CLI workspace 曾在本次工作中通过用户身份验证，但最新状态为 `needs_refresh`；Hermes 应用对应的 CLI workspace 中 bot 身份可用，user token 缺失。
- 以 Hermes 应用的 bot 身份读取原配置群的信息成功，返回普通群类型。这不是独立测试应用的验收。
- `lark-cli im +messages-reply --dry-run` 返回请求预览，没有调用发送接口。隔离临时数据库的 `doctor --mode dry-run` 中，Codex App Server、多 Agent、bot 身份及应用 ID 匹配检查通过；整体未通过，原因是 Hermes workspace 的 user token 和 scopes 无法验证，磁盘空闲比例触发告警。
- 原项目配置所指生产 SQLite 为 v8；新代码只提供 v9、v10、v11 到 v12 的迁移，未修改原库。不要直接用该生产配置启动新版本。

## 尚待验收

需要独立飞书测试应用及其测试群、应用内的 owner ID 和必要授权，才能验证长连接入站、无 @ 事件、真实话题位置及回执、转发正文、Hermes Gateway 回投和人工接手续跑。当前没有这些真实联调证据。

生产线程禁用直接网络和外部 MCP，保留工作目录内的本地执行及原生子 Agent；只读工具限定当前授权群。此前已有的 UUID 可能未注册这些工具。需要在实际部署平台验证 Codex sandbox 和工具配置，并完成独立飞书应用联调后，才能验收生产发送授权边界。
