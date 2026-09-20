# 05 · 合并转发子资源

状态：阻塞，container image 已验证可下载，但转发 file 尚未由 CLI/API 暴露。前置：会话 03 的 membership 分类已完成；会话 04 的资源/身份现场条件已核对。真实资源能力测试是代码接入的门槛。

## 目标与现有证据

`ingestion._resources()` 遇到 `merge_forward` 直接返回空列表，转发内容中的文件与图片只有文本占位。当前 ADR-0013 把子资源阻断归因于缺少 child message ID；但上游 [CLI enrichment 文档](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-message-enrichment.md)把顶层 container `message_id` 标为资源下载 ID，[SDK 问题记录](https://github.com/larksuite/oapi-sdk-python/issues/120)又报告过 `234003`。本机配置路径 `.venv/node_modules/.bin/lark-cli` 可执行，2026-09-20 版本为 1.0.56，下载与 mget 命令 help 可用；尚无当前授权与真实 container-ID 下载证据。

2026-09-21 preflight：本机 CLI 仍为 1.0.56，user 与 bot 凭据均已验证。user 在唯一受控 chat 的读取返回 8 条消息且无 `merge_forward`；bot 对同一列举调用返回 API `230002`。没有可在当前环境读取并以 container ID 下载的 image + file 样本，因此未调用下载接口。需要 owner 在获准测试 chat 提供一个当前、可由 user 读取的合并转发容器（同时含 image 与 file）；届时仍须以 bot 身份对同一 container ID 分别下载两个键，才可进入阶段 2。消息、chat、资源键和下载文件均未写入 tracked 证据。

2026-09-21 revalidation：owner 提供了当前 image + file 容器。user 能从其可见内容识别两种键；bot 用同一 container ID 下载 image 成功并完成 size/hash 核对。file 的直接下载连续两次返回 network `500`、没有输出文件；在 ignored 临时目录运行 bot `+messages-mget --download-resources` 时，返回的资源数组只包含 image，且只保存一个 image 文件。当前 blocker 因而从“缺少样本”收敛为“转发 file 没有可下载的 container 资源表示”。在 CLI/API 将该 file 暴露为可下载资源前，保留 fail-closed 占位，不进入阶段 2。

## 边界

合并转发是当前 chat 的一个容器消息。转发子项不产生独立 task、reply target 或跨源 chat fetch。资源键只能来自当前可见的容器内容；下载、配额、hash、目录隔离、原子发布和失败占位仍由现有 `ResourceProcessor` 处理。原始资源、路径、ID 和消息正文不进入 tracked 证据。

## 阶段

1. **实测能力。** 用获准的 image 与 file 合并转发样本，读取容器消息和资源键，再用容器 ID、实际身份与 scope 调 `+messages-resources-download` 到安全临时路径。可用 `+messages-mget --download-resources` 作诊断，不采用其默认目录作为生产存储。记录 CLI 路径/版本、接口结果和脱敏错误。若容器 ID 失败，在 ADR-0013 与[当前待办](../post-mvp-backlog.md)记录具体 blocker 后停止；不写猜测性接入代码。
2. **条件接入。** 能力通过后从容器可见内容提取 image/file 键，绑定 container `message_id` 并交给现有资源处理链。覆盖多 child 同 key 去重、缺 key、下载失败占位、`234003` 单资源隔离、配额与 retention；确认 routing/reply target 不因子项变化。相邻测试、ADR 与 backlog 更新组成一个独立提交；不预设 schema migration。
3. **端到端验收。** 用相同环境读取真实容器，核对资源 hash、保存路径、Operator message detail 与失败降级。将脱敏结果单独提交；若运行失败，保留降级并明确失败类型、版本与后续条件。

## 验收

聚焦运行 `tests/test_message_lifecycle.py`、`tests/test_p2_ingestion_routing.py`、资源处理与 Operator detail 相邻测试，再跑 Ruff、Pyright、全量 pytest。代码路径和真实 CLI 路径分别验收；help 或模拟测试不能代替真实资源成功。真实消息与下载文件留在 ignored 位置。
