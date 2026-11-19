# 原生运行时验证记录：2026-11-19

按先 dry-run、后真实发送的顺序，在 owner 私聊和 `bot-test` 群分别验证 Codex 直连与 Hermes Gateway。实际飞书应用已开通 `im:message.group_msg`；真实发送仅限上述测试范围。

## 本地验证

- `uv run --locked pytest -q`：**1298 通过、1 跳过**，另有 2 条依赖弃用警告。跳过项为需实际发送审批卡片的独立端到端测试。
- `ruff check .`、`ruff format --check .`、`uv run --locked --extra cards pyright` 和 `uv run --locked pre-commit run --all-files` 通过。
- Hermes v0.23.2 的飞书适配器、音频投递和会议邀请测试共 **89 通过**，包含话题后台回复补丁的测试。
- 假飞书和合成事件覆盖群与私聊准入、话题开题和移交、引用、关注、转发、任务补充、子 Agent、接手、投递核对和保留期清理；SQLite v9–v12 升级到 v13 的在途数据由本地测试覆盖。
- 真实 Codex CLI 0.167.0 与隔离 App Server 验证会话创建、恢复和卸载；dry-run 使用隔离工具配置，生产运行沿用 Codex 原生工具、网络和 sandbox 配置。
- 独立审查对照了原始需求会话。修复审查发现的问题后，复审未发现新的 P0–P3 问题；最后一轮保留期复审独立运行 237 项相关测试。

## 真实飞书：Codex 直连

- 真实应用 `doctor` 和 dry-run 通过；dry-run 生成回复预览，没有发送消息。
- 群内 @、无 @ 引用机器人回复、创建话题及原话题续接通过。`auto` 模式先确认话题回执，再在新会话执行并回复该话题。
- 群主区请求读取同群另一话题时，真实调用 `read_feishu_topic`，并在原主区回复正确结果；话题链接形式未单独验证。
- owner 在话题中开启关注后，无 @ 的相关消息触发同话题回复；取消关注后记录清除。原生子 Agent 结束后，结果回到原话题。
- owner 私聊的新请求和引用续接通过。单条转发的正文被读取；两条消息合并转发后，机器人在私聊中准确概括内容；随后引用第一条转发，机器人回到原会话并读取其内容。飞书接口返回的单条转发没有来源归属字段，因此仅验证了正文。
- 暂停会话、用原目录执行 `codex --no-daemon resume <UUID>`、退出后交回，并在飞书新一轮读取人工补充，整条链路通过。
- 生产轮次真实调用了原生 shell，并以 HTTP HEAD 访问 `example.com`，返回 200；已配置的 Figma MCP `whoami` 只读调用成功，飞书回复只包含状态。实际父任务和子 Agent 的会话记录均显示 `gpt-6-luna`。其他网络服务和 MCP 工具未逐项验证。

首次接手试验曾使用未带 `--no-daemon` 的 `codex resume`，共享 App Server 在交互进程退出后仍持有线程，项目正确显示 `needs_review`。改用隔离交互进程后，另一会话完成了上述接手与交回。

## 真实飞书：Hermes Gateway

- 独立 `coworker` profile 以 Hermes 原生 Gateway 处理 owner 私聊、群内 @ 和已有话题回复，均实际收到并发出消息。
- 原生 `delegate_task` 在群主区、私聊和话题中完成后台任务。话题测试确认结果发回原话题；切换默认配置后的验收样本中，父任务和子 Agent 均使用 `gpt-6-luna`。早期群主区和话题试验曾使用 `gpt-5.6-luna`。
- Hermes v0.23.2 原版话题后台回复曾因使用话题 ID 创建消息而失败。安装 [适配器补丁](../patches/hermes-0.23.2-feishu-topic-replies.patch)后，同一业务场景真实回投通过。补丁位于本机 Hermes 分支 `codex/feishu-topic-reply-anchor`，提交 `f5765e51`。

## 迁移与当前状态

- Hermes 从 v0.20.0 迁移至 v0.23.2 多 profile 主网关。原数据和 systemd 单元保存在 `~/.hermes/backups/native-runtimes-20261119-ifp4wnga/`，原安装保留。
- `default`、`wechatbot` 和 `coworker` 均由主网关承载。重复的飞书凭据已从前两个 profile 移除；原飞书应用凭据配置在 `coworker` profile。该 profile 工作目录现为 `~/.hermes/profiles/coworker/workspace`，实际私聊中的终端 `pwd` 与原生子 Agent 执行 `pwd` 并回投均已验证。
- 原项目生产 SQLite v8 已备份到 `data/backups/agent-v8-20261119-211634.sqlite3`，并用 v13 新库替换；原全局策略和两条聊天策略保留，历史业务数据未导入。dry-run SQLite 从 v11 升级到 v13 并同步生产策略；两库完整性检查及生产、dry-run `doctor` 通过。
- 本机配置中的 Codex 模型已设为 `gpt-6-luna`。现有策略未启用项目 Codex 同事，Codex daemon 未启动；本次 `doctor` 检查的是当前配置。
- `coworker` profile 的 `SOUL.md` 从 667 字节精简至 354 字节，原文件保存在 profile 的 `backups/SOUL-before-simplification-20261119-212220.md`。新会话使用该文件；已有会话的缓存提示在会话刷新或压缩后更新。

本次架构审查的问题与优化建议见[原生运行时架构审查](native-runtime-architecture-review-2026-11-19.md)。

跨话题引用链接、非 owner 拒绝和其他群拒绝已做本地测试，尚未逐项完成真实飞书验收。涉及其他机器人发言的无 @ 事件和真实审批卡片也未验收。
