# Post-MVP Backlog

状态：当前的 post-MVP 代办入口，依据当前代码、测试和 tracked 文档整理。

本文件只记录尚未关闭的产品/工程工作，以及需要外部环境或 owner 完成的评测闭环。P1-P21 等阶段计划保留当时的历史状态，不是当前 TODO 入口；包括 P21 在内的阶段计划不在本文件中改写。运行时评测产生的 data、配置、真实消息和报告属于 ignored 产物，不是本文件的事实源。

## 当前待办

| 优先级 | 条目 | 来源 | 下一步与完成条件 |
| --- | --- | --- | --- |
| P2 | 高流量群的 ingest 上限、lag 和恢复指标 | 群聊分析已有部分记录 | 设计已敲定（ADR-0016）：每 chat 页数/消息数上限 + 每 tick 全局时间预算；未全量 drain 时 checkpoint 不推进，溢出为 Ingest Backlog 由下一 tick 重拉（overlap+去重兜底），绝不静默截断；指标经 Operator Query slice（checkpoint 年龄/页数/消息数/drain 完成/积压标记）+ JSONL。实现 = 上限/预算落 ingestion、指标 slice、focused tests。 |
| P1 | 消息生命周期语义（剩余 reaction、合并转发和跨 chat 引用） | 群聊分析已有记录；撤回/编辑第一切片已完成（在主 worktree 未提交） | 设计已敲定：reaction 为非信号（ADR-0014）；合并转发为容器单消息、记录全部 msg_type、展开文本原样保留、子资源占位符为天花板（ADR-0013，子资源下载依赖 lark-cli 暴露 message_list）；路由永不跨 chat（ADR-0013）。实现 = NormalizedMessage 加 message_type + 路由跨 chat 守卫 + focused tests。 |
| P1 | incidental mention 与 bot membership 自愈 | 群聊分析已有记录 | 设计已敲定：incidental mention 为非信号，路由层边界已存在（owner 消息只 takeover/IGNORE），补文档与 focused tests；bot 离群 = 被动失败归因（扩展 send 路径）+ 主动探测（`im chat.members bots`）+ Effective Policy 运行时派生降级（ADR-0015），通知 owner 处置、不自动改写 Policy Store、不自动加群。实现 = send 路径归因 + 探测适配 + 降级/通知 + 测试。 |
| P2 | 长 Task Session 的 context budget 或 running summary | 群聊分析已有记录 | 先用真实长对话失败证据确定窗口、summary owner 和恢复顺序，再决定 schema、prompt 或 session 策略；不得把 metadata 直接扩进生产 prompt。 |
| P2 | activation mode 与多 active task 优先级 | 群聊分析已有记录 | 为 mention-only、thread follow-up、keyword 等入口定义明确优先级、冲突和 per-chat 配置，再实现。 |
| P2 | 后台补充背景与任务/资源重试命令 | MVP 后续列表 | 评估 /reply background 和 /retry 的权限、状态机、幂等和 owner 可见性；现有 dispatch retry 的人工恢复不能自动等价替代。 |
| P2 | 通用配置编辑和 per-user policy | MVP 后续列表 | 当前 Policy/Settings 页面不等于任意 config editor；先定义 config_change approval、审计和回滚边界。 |
| P2 | 部署与外部集成扩展 | MVP 后续列表 | LaunchAgent、systemd、Windows service、桌面/远程 console、SDK/OAuth、向量检索和更细资源分析均未纳入当前目标，按实际产品需求拆成独立决策。 |
