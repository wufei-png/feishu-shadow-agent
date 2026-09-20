# 04 · ingest 与 membership 现场验收

状态：待执行。前置：会话 03 的错误归因已验收；使用获准的测试 chat 与可恢复的 bot membership。

## 目标

S4 的有界 ingest 与 S5 的 runtime membership 已在代码和 fixture 中实现。本会话收集真实高页数/高延迟运行及 bot 离群/重入时间线，判断默认预算、恢复和告警是否满足运行要求。没有测试 chat 或授权时，记录缺少的条件并保持未验收。

## 环境与证据

本机 `config.yaml` 的 `lark_cli.path` 指向 `.venv/node_modules/.bin/lark-cli`；2026-09-20 读到版本 1.0.56，命令 help 可用。开始时重新核对实际路径、版本、授权、scope、`doctor` 与 dry-run。私有配置、消息和运行报告留在 ignored 位置；提交的验收记录只包含时间、版本、配置 hash、计数、错误类别和脱敏标识。

## 阶段

1. **ingest 追赶。** 在受控高流量消息源下观察 page/message cap、tick deadline、跨 tick backlog、checkpoint 不越过未完整取得的窗口、重启后 overlap 去重和后续 dispatch 调度。记录页数、消息数、耗时、checkpoint age、backlog 与最终追赶结果；必要时把实测预算缺陷拆成独立代码修复与复验。
2. **membership 恢复。** 在获准测试 chat 中观察 `present → absent → unknown/过期 → recovered` 的 fact、Effective Policy、资源/回复降级和 episode 通知。确认 Product Policy 不变。只在测试 chat 操作 membership；不进行真实回复发送。

## 验收

将两项脱敏时间线和环境条件写入运维验收文档，分别标注通过、失败或未验收，并做单独文档提交。测试不能替代现场证据，`doctor` 通过也不能替代上述状态变化。出现代码缺陷时只修具体问题、重跑对应链路，再更新[当前待办](../post-mvp-backlog.md)。
