import type { TFunction } from "i18next";
import type { SettingsCatalogEntry } from "./types";

type CatalogTranslation = { label: string; description: string; help?: string };

// The API catalog remains the source of truth. Chinese copy is keyed by the
// stable catalog key so wording can change without matching backend sentences.
const zhCatalog: Record<string, CatalogTranslation> = {
  "policy.global.p2p_auto_reply": { label: "私聊自动回复", description: "回复条件满足时，允许在一对一会话中自动回复。" },
  "policy.global.unknown_group_auto_reply": { label: "未配置群聊自动回复", description: "回复条件满足时，允许未单独配置策略的群聊自动回复。", help: "仅适用于产品策略中没有独立会话策略的群聊。" },
  "policy.global.default_bot_joined": { label: "默认机器人可用状态", description: "未单独配置策略的会话默认是否可使用机器人。" },
  "policy.global.default_reply_identity": { label: "默认回复身份", description: "未单独配置策略的会话默认采用的回复身份。", help: "“优先机器人”会先使用机器人；允许降级时，可改用你的用户身份。" },
  "policy.global.default_allow_user_fallback": { label: "默认允许用户身份降级", description: "优先使用机器人回复失败时，默认是否允许改用用户身份。", help: "仅在回复身份为“优先机器人”时生效。" },
  "policy.global.default_resource_download": { label: "默认允许资源下载", description: "默认是否允许保存消息中的可下载资源，供任务处理使用。", help: "控制是否可将符合条件的飞书资源保存到本地，作为助手上下文。" },
  "policy.chat.name": { label: "会话名称", description: "供操作人员识别会话的名称。" },
  "policy.chat.auto_reply": { label: "自动回复", description: "所有回复条件满足时，允许此会话自动回复。" },
  "policy.chat.bot_joined": { label: "机器人已入群", description: "机器人是否可在此会话中回复和访问资源。", help: "机器人回复和资源访问都依赖机器人已加入当前会话。" },
  "policy.chat.reply_identity": { label: "回复身份", description: "此会话发送回复时使用的身份策略。", help: "可按发送要求选择优先机器人、仅机器人或仅用户。" },
  "policy.chat.allow_user_fallback": { label: "允许用户身份降级", description: "优先使用机器人但无法回复时，允许改用你的用户身份。", help: "仅在回复身份为“优先机器人”时生效。" },
  "policy.chat.resource_download": { label: "允许资源下载", description: "允许保存此会话中的可下载资源，供任务处理使用。", help: "控制是否可将此会话的飞书资源保存到本地。" },
  "policy.status.initialized": { label: "产品策略已初始化", description: "运行时产品策略存储是否已有全局策略。" },
  "policy.status.import_diff": { label: "策略导入差异", description: "只读比较配置导入源与运行时产品策略存储。" },
  "policy.audit.history": { label: "策略审计记录", description: "审计表中最近记录的产品策略变更。" },
  "policy.import_config": { label: "导入配置策略", description: "将配置导入源中的策略值写入运行时产品策略存储。" },
  "lifecycle.approval_timeout_hours": { label: "审批超时时间", description: "待审批项在多少小时后可被显式标记为过期。", help: "只读页面会先显示为逾期；执行过期命令后，状态才会变为已过期。" },
  "lifecycle.watch_minutes": { label: "任务观察窗口", description: "任务在最近活动后继续保持活跃的分钟数。" },
  "lifecycle.closed_recall_days": { label: "已关闭任务召回窗口", description: "任务关闭后仍可被显式召回的天数。" },
  "retention.raw_message_days": { label: "敏感内容保留期", description: "任务观察期结束后，消息链与日志载荷继续保留的天数。" },
  "retention.resource_days": { label: "资源保留期", description: "已下载资源文件的保留天数。" },
  "daemon.tick_interval_seconds": { label: "守护进程轮询间隔", description: "守护进程两次轮询之间的秒数。" },
  "daemon.overlap_seconds": { label: "消息重叠窗口", description: "拉取飞书消息时额外向前回看的秒数。" },
  "health.interval_seconds": { label: "健康检查刷新间隔", description: "两次完整运行健康检查之间的秒数。" },
  "health.retry_interval_seconds": { label: "健康检查重试间隔", description: "严重健康故障后再次检查前等待的秒数。" },
  "health.timeout_seconds": { label: "健康检查超时", description: "健康探测默认允许的最长秒数。" },
  "storage.sqlite_path": { label: "SQLite 数据库路径", description: "当前配置的 SQLite 数据库文件路径。" },
  "storage.resource_dir": { label: "资源目录", description: "已下载资源的本地存储目录。" },
  "storage.max_resource_bytes": { label: "单个资源大小上限", description: "单个下载资源允许的最大字节数。" },
  "storage.max_resource_dir_bytes": { label: "资源目录容量上限", description: "资源目录允许占用的最大总字节数。" },
  "agent_backend.provider": { label: "Agent 提供方", description: "当前配置的 Agent 后端提供方。" },
  "agent_backend.max_attempts": { label: "Agent 最大尝试次数", description: "每次可重试 Agent 调用在所有工具权限模式下允许的最大尝试次数。", help: "只读与完全访问使用同一限制；完全访问重试可能重复执行工具副作用。" },
  "agent_backend.working_dir": { label: "Agent 工作目录", description: "Agent 子进程和新任务会话使用的工作目录。", help: "空值使用配置文件所在目录；相对路径也以该目录为基准。已有任务继续使用创建时保存的工作目录。" },
  "agent_backend.config_scope": { label: "Agent 配置范围", description: "Agent CLI 是加载用户级全局配置，还是以隔离模式运行。" },
  "agent_backend.auto_context": { label: "Agent 自动上下文", description: "Agent CLI 是否加载隐式规则、记忆和默认技能上下文。" },
  "agent_backend.explicit_context.paths": { label: "显式上下文路径", description: "新任务会话提示中列出的非原生技能绝对路径。" },
  "agent_backend.hermes.skill_paths": { label: "Hermes 原生技能路径", description: "通过 --skills 传给任务会话的 Hermes 原生技能路径。" },
  "agent_backend.codex.skills": { label: "Codex 原生技能", description: "仅在创建任务会话时请求的 Codex 原生技能名称。" },
  "agent_backend.hermes.mode": { label: "Hermes 健康检查模式", description: "Hermes 健康检查使用的模式；任务处理仍使用本地 CLI。" },
  "agent_backend.hermes.model": { label: "Hermes 模型", description: "可选的 Hermes 模型覆盖值。" },
  "agent_backend.hermes.provider": { label: "Hermes 提供方", description: "可选的 Hermes 提供方覆盖值。" },
  "agent_backend.hermes.router_max_turns": { label: "Hermes 路由最大轮次", description: "路由调用允许的最大 Hermes 工具迭代轮次。" },
  "agent_backend.hermes.session_max_turns": { label: "Hermes 会话最大轮次", description: "任务会话调用允许的最大 Hermes 工具迭代轮次。" },
  "agent_backend.hermes.timeout_seconds": { label: "Hermes 超时", description: "Hermes Agent 调用的可选超时秒数；空值表示不限制探索时长。" },
  "agent_backend.hermes.path": { label: "Hermes 可执行文件", description: "可选的 Hermes 可执行文件路径。" },
  "agent_backend.hermes.health_url": { label: "Hermes 健康检查地址", description: "仅用于健康检查的可选 Hermes HTTP 地址。" },
  "agent_backend.hermes.api_key_env": { label: "Hermes API 密钥环境变量", description: "Hermes HTTP 健康检查认证使用的环境变量名称。" },
  "agent_backend.codex.model": { label: "Codex 模型", description: "可选的 Codex 模型覆盖值。" },
  "agent_backend.codex.reasoning_effort": { label: "Codex 推理强度", description: "可选的 Codex 推理强度覆盖值。" },
  "agent_backend.codex.timeout_seconds": { label: "Codex 超时", description: "Codex Agent 调用的可选超时秒数；空值表示不限制探索时长。" },
  "agent_backend.codex.path": { label: "Codex 可执行文件", description: "可选的 Codex 可执行文件路径。" },
  "agent_backend.claude_code.model": { label: "Claude Code 模型", description: "可选的 Claude Code 模型覆盖值。" },
  "agent_backend.claude_code.timeout_seconds": { label: "Claude Code 超时", description: "Claude Code Agent 调用的可选超时秒数；空值表示不限制探索时长。" },
  "agent_backend.claude_code.path": { label: "Claude Code 可执行文件", description: "可选的 Claude Code 可执行文件路径。" },
  "tool_permissions": { label: "工具权限", description: "Agent 后端使用的工具权限模式。" },
  "owner.open_id": { label: "所有者 open_id", description: "当前配置的飞书所有者 open_id。" },
  "owner.name": { label: "所有者名称", description: "可选的、供操作人员识别的所有者名称。" },
  "lark_cli.path": { label: "lark-cli 可执行文件", description: "可选的 lark-cli 可执行文件路径。" },
  "lark_cli.timeout_seconds": { label: "lark-cli 超时", description: "lark-cli 子进程调用的超时秒数。" },
  "logging.jsonl_path": { label: "JSONL 日志路径", description: "当前配置的结构化日志文件路径。" },
  "logging.level": { label: "日志级别", description: "运行时记录日志的最低级别。" },
  "logging.console": { label: "控制台日志", description: "是否同时将便于阅读的运行日志写入标准错误输出。" },
  "logging.text_path": { label: "文本日志路径", description: "可选的纯文本日志文件路径。" },
  "debug.save_full_agent_io": { label: "保存完整 Agent 输入输出", description: "仅用于调试：持久化完整 Agent 提示和输出。" }
};

function resourceKey(entryKey: string, field: keyof CatalogTranslation): string {
  return `catalog.${entryKey}.${field}`;
}

export function chineseCatalogResources(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(zhCatalog).flatMap(([entryKey, copy]) =>
      (Object.entries(copy) as Array<[keyof CatalogTranslation, string]>).map(([field, value]) => [resourceKey(entryKey, field), value])
    )
  );
}

export function catalogText(t: TFunction, entry: SettingsCatalogEntry, field: keyof CatalogTranslation): string {
  return t(resourceKey(entry.key, field), { defaultValue: entry[field] ?? "" });
}
