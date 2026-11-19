# 原生运行时架构审查：2026-11-19

审查基于 `dbf0281`，对照 [实施方案](../plans/ai-coworker-native-runtimes.md)和原生运行路径。以下问题均尚待修复。复现使用隔离 SQLite、假飞书客户端或内存 stub，没有发送飞书消息。

## 已确认的问题

1. **P2：关注状态可能使用已修改的请求。** [关注写入](../../src/feishu_shadow_agent/coworker_native.py#L1095)发生在[发送前快照校验](../../src/feishu_shadow_agent/coworker_native.py#L1936)之前。模型执行期间，owner 把“关注这个话题”改为“不用关注了”，假客户端复现中 watch 仍被创建，答复则因源消息变化进入 `needs_review`。取消关注同样需要在状态变更前校验依据。
2. **P2：发送前读取故障会阻塞会话。** [快照读取](../../src/feishu_shadow_agent/coworker_native.py#L2018)发生超时时，消息尚未发送；[通用异常处理](../../src/feishu_shadow_agent/coworker_native.py#L1986)仍标记 `needs_review`。假客户端复现中实际发送为零，后续同话题消息保持排队。应区分发送前可恢复故障和发送结果不明，并对前者使用原幂等键进行有限重试。
3. **P3：保留期清理会丢失待移交进展。** [清理逻辑](../../src/feishu_shadow_agent/retention.py#L257)会清空已发送的接单正文，即使对应 transfer 事件仍在排队；[移交处理](../../src/feishu_shadow_agent/coworker_native.py#L1169)需要该正文作为新会话的已知进展。假客户端复现了正文在 `protect_pending=True` 下被清空，后续模型轮缺少该进展。
4. **P3：根轮次映射持续增长。** [通知处理](../../src/feishu_shadow_agent/coworker_native.py#L197)为每个根轮次写入 `_root_turn_origins`，完成后没有释放。内存模拟 1,000 次轮次开始和完成后仍保留 1,000 项；实际 RSS 增长尚未测量。清理时须保留延迟子 Agent 通知所需的来源关联。

旧模式创建且未注册群历史动态工具的 Codex UUID，恢复后仍缺少这些工具；手册已记录这一迁移限制。当前 Codex 0.167.0 的实验性 App Server schema 支持在 `thread/start` 注册动态工具，但 `thread/resume` 不提供该字段，因此保留 UUID 与为旧线程补注册工具需要进一步确定方案。

## 复杂度和效率

Hermes 项目入口直接启动指定 profile，飞书连接、会话和子 Agent 由 Gateway 管理。Codex 路径则由项目维护事件队列、来源快照、跨话题回执、子 Agent 完成通知和人工接手；这些职责对应产品要求，但实现已集中在约 2,136 行的 `coworker_native.py` 和约 1,848 行的 `coworker_store.py`，不宜再添加平行状态机。

关注消息合批后，当前代码逐条读取来源、发送前又逐条读取校验。按一条 leader 加 20 条全部成功合批的补充消息、且没有额外上下文计算，两处共需 **41 次串行消息读取**；这是调用链静态计数，不是延迟实测。现有 `messages-mget` 支持最多 50 条，可考虑分批读取并逐项保留原校验。关联补充判断目前为每次判断启动短生命周期 App Server；复用是否值得需要先测量启动耗时，并保留临时线程和工具隔离。

## 提示词

Hermes 的 `SOUL.md` 只在身份槽加载一次，不能把 profile、默认 home 和安装目录中的相同文件计为三份注入。profile 的身份提示已从 667 字节精简到 354 字节，保留原有身份和回答要求；新的原生会话加载该文件。原始文件已备份。

Codex [关注提示](../../src/feishu_shadow_agent/coworker_native.py#L960)提到 owner 可关注或取消，但[执行条件](../../src/feishu_shadow_agent/coworker_native.py#L1060)还要求群内已有话题和非空确认文本；`watch` 显式提供时长时，该时长须大于零。模型在不适用的位置选择关注动作或未填写确认文本时，事件会进入 `needs_review`；尚未观察到真实模型触发。提示可明确这些条件，并对不适用的位置使用普通答复。

当前 `gpt-6-luna` 会加载上游任务完成、工具使用和执行纪律指导，其中工具使用指导约 824 字符，与另两段部分重复。可通过原生 `agent.tool_use_enforcement: false` 做受控 A/B；该开关改变模型指导，应先验证实际任务效果。项目 Codex 群后台记忆整理在新配置中默认关闭，旧 SQLite 已存设置保留；关闭时不计入每条普通消息的提示词。
