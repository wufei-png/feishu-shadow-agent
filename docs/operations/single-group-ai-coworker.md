# 单测试群 AI 同事：运行与验证

本功能默认关闭。下面的配置示例使用占位 ID，需先选择独立测试群、配置飞书应用并完成授权；本地测试通过不代表真实飞书权限和机器人互操作已经验证。

Codex 轻量直连见 [Codex 直连 AI 同事](codex-native-coworker.md)，Hermes 原生网关见 [Hermes Gateway AI 同事](hermes-coworker.md)。本文以下描述旧 `daemon` 的 `agent_direct` 和 `project` 模式。

## 接入准备

1. 准备一个具备机器人能力的飞书自建应用，发布应用并将其加入独立测试群；订阅 `im.message.receive_v1`，使用长连接接收事件。伙伴机器人也需加入群，且其自身配置允许接收并处理委派。
2. 为应用申请消息读取、机器人发送、群信息和会话历史等必要权限。连续缓存和持续理解需要覆盖非 @ 事件。接收其他机器人 @ 需要 `im:message.group_at_msg.include_bot:readonly`，双方应用均需核对各自权限和处理配置；对方不 @ 或事件未送达时，不能保证自动触发。详见[飞书消息概述](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1)和[官方 Channel SDK](https://github.com/larksuite/channel-sdk-python/blob/main/docs/reference.md)。
3. 安装依赖：`uv sync --locked --extra cards`。通过现有 `lark-cli` 授权流程配置相同应用及用户身份，使用 `lark-cli auth status --verify --json` 检查；长连接读取 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，两个值由环境或凭据管理提供，不写入配置文件。`doctor` 检查长连接应用 ID 和 `lark-cli` 的应用 ID 是否一致，但不能证明事件订阅或每个 bot scope 已生效。
4. 同一飞书应用的多条长连接采用集群分发，不是广播。项目将同一应用的审批卡片和消息接入同一连接；不要同时运行另一个使用同一应用的 Hermes 网关、`lark-cli event consume` 或其他 daemon，除非确实接受事件被分流。做 dry-run 时也要考虑是否已有生产连接。

## 示例配置

在现有 `config.yaml` 中调整以下字段；其他必需配置继续沿用现有文件。`oc_test_group`、`ou_box` 仅为占位符，伙伴 open ID 必须属于当前应用的身份空间。

```yaml
agent_backend:
  provider: codex
  working_dir: null
  codex:
    skills: [lark-im]

chats:
  oc_test_group:
    name: AI 同事独立测试群
    auto_reply: true
    bot_joined: true
    reply_identity: bot
    allow_user_fallback: false

coworker:
  enabled: true
  chat_id: oc_test_group
  provider: codex
  send_mode: agent_direct
  trigger_mode: mention
  history_source: live_on_demand
  history_limit: 30
  max_concurrent_topics: 3
  subagent_timeout_seconds: 3600
  partner_bots:
    Example Partner:
      open_id: ou_box
      description: 处理委派的子任务并反馈结果。
  delegation_timeout_seconds: 900
  delegation_rate_limit: 5
  delegation_rate_window_seconds: 900
  memory_maintenance: false
  shared_memory_path: data/coworker-shared-memory.md
  memory_maintenance_time: "00:00"
```

`coworker` 是 Policy Import Source：运行时读取 SQLite Product Policy Store。**一次 daemon 启动只运行一种业务模式**：生效策略 `coworker.enabled: true` 启动 AI 同事，`false` 启动原影子模式。切换时先停止 daemon，修改配置，以 `policy import-config --replace` 导入已有策略，再重启；`coworker status` 可查看同事策略。运行中改变模式、同事群、后端或发送方式会阻断当前进程继续处理，必须重启。两个独立进程没有全局互斥锁，也不能视为安全的并行方式：共用飞书应用时事件会分流，共用数据时还有并发影响。

`partner_bots` 按名称配置飞书伙伴的 `open_id` 和可选 `description`。职责描述用于模型判断何时向伙伴委派，可以在本地 `config.yaml` 写业务约定；项目通用配置和示例不包含具体业务。原有 `名称: open_id` 格式仍可使用。导入后的职责会保存在运行时策略中，并在同事轮次的提示词中提供给模型；它是工作指引，不会限制模型自行调用其他工具。原生子 Agent 由 Codex 执行并向主 Agent 返回结果，主 Agent 结合当前对话决定是否在群内汇报；飞书伙伴是通过群消息协作的独立机器人。

测试群仍需要 `chats` 中的明确群策略。普通影子助手使用 `agent_backend` 所选后端；AI 同事默认单独选择 Codex，它的调用继承原生配置、规则、权限和记忆，保持原生指令，不强制只读，也不强制 `--dangerously-bypass-approvals-and-sandbox`。非交互调用有 `coworker.agent_timeout_seconds` 上限，默认为 600 秒。

Codex 同事使用常驻 `codex app-server --stdio`；需确认所用 Codex 版本支持 App Server 和原生多 Agent，且运行环境的 `multi_agent` 功能已启用。`doctor` 检查 App Server 命令并提示多 Agent 功能状态，但不能证明所选模型一定会委派或该版本的事件协议与当前实现完全兼容。若 Codex 请求交互式工具审批，项目不会自动批准；请按原生配置处理。Hermes 仍可选作同事后端，但当前是单次调用，不支持此处描述的 Codex 后台子 Agent、运行中补充或跨话题并行。

同事模式启动时需要健康的飞书事件连接；连接未就绪时 daemon 以健康失败退出。运行中 Codex App Server 意外退出时，消息继续入队，Console 和健康检查显示失败，需重启 daemon；无法确认结果的执行仍需核对。成功重启或切换到不使用该服务的模式后，旧的 App Server 失败状态会清除。

同事模式目前复用 `agent_backend.codex.skills` 和 `agent_backend.explicit_context.paths`：新建 Codex 会话时追加配置的技能引用和路径，恢复已有会话时不重复追加。它们与原影子模式的配置不是两套。`lark-im` 和 `lark-doc` 在同事提示中被列为可用技能，但仅靠提示词不能证明运行身份的 Codex 已发现技能或具备飞书权限；需在同一运行环境验证。

`send_mode` 在 daemon 启动时固定：默认 `agent_direct` 让模型自行使用 bot 身份发送，`project` 使用原有结构化动作与项目代发。修改策略后重启 daemon；如果运行期间改动该字段，尚未处理的事件保持排队而不是静默切换。模式切换不更换后端会话，每轮只注入当前模式的发送说明；会话旧内容可能干扰模型，实际行为仍须联调。旧策略缺少该字段时沿用 `project`，显式导入新策略才切换。直发的最终文本不会被项目再次发送。

直发提示词说明测试群内的自动回复授权，但本机 `lark-im` 回复技能仍有逐条确认的通用说明，后端自身的权限、审批和技能规则也可能阻止或暂停发送；需在实际运行环境验证。读取飞书 Project 工作项使用独立 `meegle` CLI 和其技能的认证流程；读取群消息及飞书云文档使用相应 `lark-*` 技能和 `lark-cli`。确认 daemon 运行身份能找到工具和技能，且 CLI 应用身份与订阅事件的应用一致。

`trigger_mode` 可选 `mention` 或 `followups`，`history_source` 可选 `live_on_demand` 或 `local_cache`，是互相独立的两项。触发后程序只注入当前消息及定位信息，Agent 自行决定是否读取历史。`live_on_demand` 提示 Agent 使用 `lark-im` 和 `lark-cli`；`local_cache` 保存实际收到的消息，向 Agent 提供只读缓存查询命令，可按话题、关键词和分页查询，按发送时间排序；缺少发送时间时使用接收时间。首次处理话题或连接重建后，本地缓存仍尝试有限补拉；查询结果会说明缓存范围及已知缺口。`history_limit` 是缓存补拉和查询的默认页面大小，不是模型工具访问飞书的硬上限。`followups` 只对已有会话的话题的后续消息启动模型判断，不要求模型每条都回复。模型继承原生权限，所选来源是工作指引，不阻止它自行调用其他工具。

前台提示按 Markdown 分为身份、工作方式、子 Agent 协作、由项目确定的本轮发送方式、资料与工具、飞书伙伴、本轮消息或内部事件。直发时提示按原消息所在位置回复，并使用 `lark-im` 处理话题回复；项目代发由项目选择并检查回复位置。提示区分群聊历史与提炼后的长期记忆，核对最新进展时回看历史；Agent 自行决定是否回看或读取。消息正文单独放入文本块，保留原文；按位置提供群、消息和可用话题 ID。只有本地缓存模式提供缓存查询命令，直发模式提供幂等键前缀；记忆只提供文件路径，不再每轮附带完整写入命令。项目代发需要结构化结果：Codex 的 Schema 通过 App Server 协议传递；Hermes 后端没有同样的 Schema 参数，因此会把输出格式作为 Markdown 中的代码块提供。模型实际遵循情况仍需通过场景测试验证。

```bash
uv run --locked python -m feishu_shadow_agent policy import-config --config config.yaml --replace
uv run --locked python -m feishu_shadow_agent doctor --config config.yaml --mode dry-run
uv run --locked python -m feishu_shadow_agent daemon --config config.yaml --dry-run
uv run --locked python -m feishu_shadow_agent coworker status --config config.yaml --mode dry-run
```

dry-run 使用隔离的 SQLite、日志、资源目录和项目共享记忆文件（文件名增加 `.dry-run`），项目发送路径只生成预览。直发模式提示模型对发送命令使用 `--dry-run`，但项目无法强制拦截模型自行调用的外部工具；测试直发时应另用模拟工具或隔离凭据。Codex 的原生环境和它自行调用的外部工具由原生权限决定。完成真实应用和群的准备、doctor 与 dry-run 后，再按测试群策略启动正式 daemon 验证。

## 运行语义和故障恢复

- AI 同事启动时不扫描或重试影子模式任务；项目代发只处理同事来源的发送与飞书私聊、卡片审批，直发不运行项目代发队列。影子模式只处理影子来源的队列与审批。同事的飞书审批需启用已有的审批入口；直接调用模型工具仍受其原生权限约束。
- 未运行模式的待办保留，审批截止时间继续流逝，切回后过期项会标记过期。daemon 的自动保留清理保护待处理的发送、审批、重试和同事事件及关联内容，其余到期数据仍可清理。操作员手动执行 `retention prune` 仍遵循原有规则，可能清理暂停工作的内容。长时间未处理的飞书私聊审批指令可能超出轮询窗口，切回后可重新发送指令或使用操作台。
- 普通群主消息按群共享后端会话，话题消息按话题共享；不因闲置自动重置。启动时以 bot 身份读取群信息中的 `chat_mode` 和 `group_message_type` 判断，修改群消息模式后须重启 daemon。普通群使用群历史，话题使用话题历史。群类型及事件字段仍需真实联调验证；模型收到的是后端保留的会话、本轮选择的消息、原生上下文及按需工具查询，不是群内所有历史的自动全集。
- 旧版本曾将普通群的独立主消息分配到不同会话，这些历史会话不会被自动合并；新规则会创建群共享会话，并可通过群历史回看补充上下文。之后切换发送方式会复用这个共享会话。
- 默认消息事件先同步写入队列再确认。Codex 下不同话题最多同时运行 `max_concurrent_topics` 个主轮，默认 3；同一话题仍只有一个主轮。该设置不限制所有原生子 Agent 的总数，子 Agent 的数量还取决于 Codex 原生配置。主 Agent 根据任务独立性和已有工作决定直接处理、启动子 Agent，或将补充交给已有子 Agent；程序不按每条 @ 强制派发。
- 同话题主轮运行时，新的触发消息尝试通过 `turn/steer` 送入主轮；主轮已结束时则排队作为下一轮。服务器接受补充只表示已接收，不代表模型或子 Agent 已执行；发送结果不明确时转为 `needs_review`，不自动重发。主轮已结束但子 Agent 还在运行时，新消息仍可启动主会话的新轮，由主 Agent 决定是否联系该子 Agent。子 Agent 结束后项目持久化内部完成事件，恢复同一主会话，由主 Agent 读取结果并决定是否汇报。
- 项目对主会话直接启动的子 Agent 提供完成唤醒；如果原生配置允许子 Agent 再次派发，下级结果应由其直接父 Agent 等待和汇总，项目不会单独唤醒这个父 Agent。恢复或交互可能产生额外完成事件；主 Agent 应判断结果是否已经汇报，直发模式仍无法由项目保证消息不重复。
- 模型启动前的临时消息读取失败按有界退避重试；重试耗尽后记录待核对，但不永久阻塞同一话题。模型调用后可能已执行本机副作用，超时或结果不明确会进入 `needs_review`，不会自动重跑。重启时只自动恢复同机已退出进程的可识别任务；已启动子 Agent 的结果不明时也进入待核对，不假设任务成功或自动再执行。所有者不明或仍存活的任务不会被抢占。
- 正常停止时先停止事件连接，再等待已启动主轮和后台记忆 Agent 完成；随后等待原生后台子 Agent。运行健康状态允许、且不会越过同话题中更早排队的消息时，项目处理完成通知；否则将通知保留到重启后。主轮与记忆轮受 `agent_timeout_seconds` 约束；等待后台子 Agent 的总时间由 `subagent_timeout_seconds` 控制，默认 3600 秒。超过后项目尽力中断并将仍未确认的子任务标为待核对，再关闭 App Server；已发生的动作和自行脱离进程管理的工作不能撤销。
- `coworker status` 显示会话、待处理事件、子 Agent、伙伴委派和缓存缺口。确认 daemon 和相关后台进程已停止并检查实际副作用后，可执行 `coworker discard-event --event-id <编号> --daemon-stopped`；这会丢弃该次事件，不会重放它。子 Agent 结果不明时应检查其实际工作，再丢弃对应待核对事件；排队中的子 Agent 完成通知也可显式丢弃。处理完该话题的待审事件、运行中子 Agent 及待完成委派后，可执行 `coworker reset-session --session-id <编号> --daemon-stopped`，普通排队消息会保留并在新会话中处理。`--daemon-stopped` 是操作员声明，不会自动停止或检测进程。上述命令同样支持 `--mode dry-run`。
- `project` 代发模式由项目构造伙伴 @ 和协作 ID，维护匹配、超时及循环限制。`agent_direct` 由模型直接 @ 配置伙伴，约定携带任务标识并请求伙伴 @ 回本机器人；不会自动生成项目委派记录，也不保证超时跟踪或防循环。两种模式均不能假定伙伴一定收到或回传。
- 项目在执行模型前用 bot 身份读取触发消息的当前正文；若接口提供最新 @ 信息，也会重新判断是否触发。项目代发在发送前再次核对正文指纹、可用 `update_time` 和删除状态，并重新检查群策略；模型自行发送不经过这些检查。项目自己的历史读取不会悄悄改用用户身份，模型调用原生工具则取决于其权限和指令。平台读取与发送不是同一个事务。当前未订阅编辑和撤回事件，本地缓存可能滞后，此前已进入后端会话的内容也不会自动消失。
- 明确要求记住时，由主 Agent 直接使用工具维护共享记忆。后台整理默认关闭；设置 `memory_maintenance: true` 后，每天本机时间 `00:00` 启动独立 Agent，可通过 `memory_maintenance_time` 调整。错过时下次启动合并补跑，旧 SQLite 策略保持原值。Agent 可按上次成功时间跨话题搜索并维护共享记忆。普通失败在下个周期重试；异常中断或策略变更导致状态不明时进入 `needs_review`，检查后在 daemon 停止时执行 `coworker reset-memory-review --daemon-stopped`。

现有 Console 的 AI 同事页面区分配置文件、SQLite 生效策略和可确认的运行进程，展示会话、事件、子 Agent、缓存缺口和后台记忆状态。首次使用仍需导入同事策略；页面可调整触发方式、历史来源和整理时间，也可在 daemon 停止后调整模式、目标群、后端和发送方式并重启。修改写入 SQLite 并记录审计，不修改 `config.yaml`，后续 `policy import-config --replace` 会按文件覆盖。恢复待核对事件、会话或记忆状态需先确认 daemon 及相关后台进程已停止；页面展示的进程状态来自项目心跳。模型直发的消息和伙伴委派不保证有项目侧记录。
- 共享资料是一份通用文件，不为每个群单独建长期记忆；同时保留 Codex/Hermes 的原生记忆。它可能在后续其他会话中被读取，应区分发言者、时间与来源。可通过 `shared_memory_path` 选择文件位置；配置文件路径相对于 `config.yaml` 所在目录。项目保留策略会清理过期队列内容，但不会自动清除后端自己维护的历史或长期记忆。
- 现有 SQLite v9、v10 和 v11 会在初始化时通过事务升级到 v12，保留原有数据；升级失败会回滚。其他不受支持的版本仍会拒绝打开。对重要运行数据先保留备份。
- 程序本轮只提供当前消息，不重复注入最近历史或共享记忆正文；恢复的 provider 会话仍可能包含过去的消息、工具调用及原生上下文。图片和文件只提供可识别的资源引用，不会自动下载，模型需要时可使用现有工具读取。直发模式中，Agent 正常结束不证明飞书消息已发送；审计记录标为未验证。

## 本地事件回放

可以从当前同事群已保存的消息重建输入，在隔离的临时 SQLite 中回放触发和上下文选择。导出文件包含群消息正文，权限设为 `0600`，不会覆盖已有文件；请放在忽略提交的私有目录。`app-id` 和 `bot-open-id` 必须是接收这些消息的同一个应用身份。

```bash
mkdir -p data/evals
uv run --locked python -m feishu_shadow_agent coworker export-replay \
  --config config.yaml --app-id cli_example --bot-open-id ou_example \
  --output data/evals/coworker-snapshot.json --limit 100
uv run --locked python -m feishu_shadow_agent coworker replay \
  --snapshot data/evals/coworker-snapshot.json \
  --trigger-mode mention --history-source local_cache
```

回放使用假飞书和返回 `silent` 的假后端，不连接飞书或模型服务，不执行发送；报告列事件状态、当前消息 ID、是否续轮及所选历史来源。它不会模拟 Agent 自主搜索历史或判断是否需要回复。可更换两个配置参数比较四种组合；由 SQLite 重建的事件并非原始飞书事件，实时回看模式下未入库的讨论、已有模型记忆、真实模型决策及伙伴委派都不会被复现。也可以提供相同 JSON 结构的原始 SDK 事件快照；普通群快照应包含 `chat_layout: group` 或给回放命令传 `--chat-layout group`，不要把 `lark-cli event consume` 的扁平化输出直接当成原始 SDK 事件。

## 验证范围

自动测试使用假飞书客户端和合成内容覆盖话题共享、子 Agent 完成唤醒、补充消息、跨话题并行、停止与中断恢复、四种历史配置、伙伴关联、审批、记忆和保留策略。也已用隔离临时目录、假飞书和 `codex-cli 0.167.0` 的真实 App Server 验证主轮结束后子 Agent 继续运行、完成后唤醒原主会话，及完成后重启 App Server 再读取结果；单独验证了运行中补充与恢复旧 Codex 会话。这些测试不代表不同版本、模型和原生配置均具备相同行为。独立测试群的权限、事件覆盖、真实发送、其他机器人是否响应、附件读取和长时间稳定性仍需联调，不能用本地测试代替。
