# 03 · 收紧 bot membership 错误归因

状态：已完成。前置：会话 01 已完成；会话 02 可先完成以保护后续合并。代码、回归和现场验收结果见[当前待办](../post-mvp-backlog.md)的会话 03、04 条目。

## 目标与现有证据

`membership.bot_membership_error()` 目前把 `error`、`stderr`、`stdout` 中任意 `234002`、`234040` 或 `invisible` 子串当作离群信号；`ingestion.py` 与 `dispatcher.py` 随后可能把 runtime fact 写成 `absent`。但[飞书下载图片接口](https://open.feishu.cn/s/61BYfeQRQ0s)把 `234002` 定义为鉴权失败。分类必须按实际 endpoint、身份与结构化错误确定；不确定失败不得覆写为已确认离群。

## 契约

- 只有具体 API 已确认的“不在当前 chat”错误，或成功的 member probe，才能产生 `absent`。scope/auth、资源不匹配、超时、CLI/JSON 错误维持原失败归因；probe 失败得到 `unknown`。
- runtime fact 叠加到 Effective Policy；不修改 Product Policy，不产生 Policy Audit，不自动拉 bot 入群。确认离群和恢复各按 episode 通知一次；旧 fact 过期后不被当作永久缺席。
- 发信结果不确定时仍走现有人工恢复，不因新的错误分类自动重发。

## 阶段

1. **错误分类。** 读 `membership.py`、`feishu/lark_cli.py` 和相关 API 契约，定义 endpoint 专属的结构化分类与安全默认值；用 auth/scope、资源不存在、确认离群、纯文本误命中和解析失败 fixture 验证后提交。
2. **接入与诊断。** 更新 `ingestion.py`、`dispatcher.py` 的调用路径，检查 probe 排程、fact 过期、Effective Policy 与通知去重；更新 ADR-0015 和必要的 Operator 诊断，跑相邻集成测试后提交。

## 验收

运行 membership、ingestion、dispatcher、policy 相邻 pytest，Ruff、Pyright 与全量 pytest。确认未知错误不能写 `absent`、确认缺席会降级、恢复可解除降级，且发信安全门保持原语义。真实 test-chat 离群/重入时间线由[会话 04](04-runtime-acceptance.md)验收；本会话的 fixture 结果不算现场通过。更新[当前待办](../post-mvp-backlog.md)。
