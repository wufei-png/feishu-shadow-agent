# Post-MVP Backlog

状态：当前的 post-MVP 代办入口，依据当前代码、测试和 tracked 文档整理。

本文件只记录尚未关闭的产品/工程工作，以及需要外部环境或 owner 完成的评测闭环。P1-P21 等阶段计划保留当时的历史状态，不是当前 TODO 入口；包括 P21 在内的阶段计划不在本文件中改写。运行时评测产生的 data、配置、真实消息和报告属于 ignored 产物，不是本文件的事实源。

## 当前待办

| 优先级 | 条目 | 来源 | 下一步与完成条件 |
| --- | --- | --- | --- |
| P0（外部闭环） | P21 DocMate Task Session 真实评测闭环 | 已有 P21 计划；代码部分已完成 | 在目标环境刷新 capture/golden/config/owner 一致性，完成 owner 标注与 promote，跑 fresh baseline，再做一次单变量对照；详见 [P21 评测计划](p21-task-session-docmate-eval.md)。 |
| P2 | 高流量群的 ingest 上限、lag 和恢复指标 | 群聊分析已有部分记录 | 明确每 tick 的消息上限、分页超时/积压策略和可观测指标；不得以静默截断代替恢复。当前仅有完整分页 drain 和 checkpoint 安全，不代表已解决高流量成本。 |
| P1 | 消息生命周期语义 | 群聊分析已有记录 | 定义撤回、编辑、reaction、合并转发和跨 chat 引用在 normalizer、上下文和 routing 中的行为，并补 focused tests。 |
| P1 | incidental mention 与 bot membership 自愈 | 群聊分析已有记录 | 区分 owner 作为发言者时对他人的 incidental mention；补充 bot 离群/下载失败后的探测、提示和人工策略边界。 |
| P1 | closed recall 的确定性 shortcut | 群聊分析已有记录；当前仍有 recall router placeholder | 本轮已收敛为候选安全边界：仅 `closed` 进入历史召回；各类客观证据仅用于构造候选，唯一候选不绕过 Router，继续由 Router 决定 `reopen_task` / `new_task` / `ignore` / `ambiguous`；已覆盖唯一结构命中、唯一弱命中、多候选冲突、无候选及审计回归。若未来仍需减少 Router 调用，需基于真实成本或误归属证据另行决策。 |
| P1 | TaskProcessingService 进一步拆分 | 本次依据代码规模推断 | processing.py 当前约 1800 行。先按行为边界提取小模块并保持现有 contract/test 不变，再决定是否继续拆分；没有明确收益前不做纯重排。 |
| P2 | 长 Task Session 的 context budget 或 running summary | 群聊分析已有记录 | 先用真实长对话失败证据确定窗口、summary owner 和恢复顺序，再决定 schema、prompt 或 session 策略；不得把 metadata 直接扩进生产 prompt。 |
| P2 | activation mode 与多 active task 优先级 | 群聊分析已有记录 | 为 mention-only、thread follow-up、keyword 等入口定义明确优先级、冲突和 per-chat 配置，再实现。 |
| P2 | 后台补充背景与任务/资源重试命令 | MVP 后续列表 | 评估 /reply background 和 /retry 的权限、状态机、幂等和 owner 可见性；现有 dispatch retry 的人工恢复不能自动等价替代。 |
| P2 | 通用配置编辑和 per-user policy | MVP 后续列表 | 当前 Policy/Settings 页面不等于任意 config editor；先定义 config_change approval、审计和回滚边界。 |
| P2 | 部署与外部集成扩展 | MVP 后续列表 | LaunchAgent、systemd、Windows service、桌面/远程 console、SDK/OAuth、向量检索和更细资源分析均未纳入当前目标，按实际产品需求拆成独立决策。 |
