# 首次准备与预演

这条路径使用一个飞书账号、一个自建应用和一个测试群。先在本地预演，再由你决定是否启动生产发送。示例命令在项目根目录执行；真实凭证、授权链接、消息和本地数据不要提交到 Git。

## 1. 安装本地工具

需要 Python 3.11 或以上、Node.js 16 或以上，以及项目要求的 uv 0.12.4。若已经安装可跳过对应步骤。下面使用独立目录安装工具，不需要覆盖系统 Python：

```bash
python3 --version
node --version
python3 -m venv "$HOME/.local/feishu-shadow-uv"
"$HOME/.local/feishu-shadow-uv/bin/python" -m pip install 'uv==0.12.4'
export PATH="$HOME/.local/feishu-shadow-uv/bin:$PATH"
uv --version

npm install --prefix "$HOME/.local/share/feishu-shadow-cli" '@larksuite/cli@1.0.96'
export PATH="$HOME/.local/share/feishu-shadow-cli/node_modules/.bin:$PATH"
lark-cli --version

uv sync --locked
cp -n config.example.yaml config.yaml
```

以上飞书 CLI 版本的命令参数已用本项目测试环境的 help 核对；真实飞书授权和服务端行为仍需要在你的租户中验证。新开终端后需重新设置上面的 `PATH`，或把对应可执行文件的绝对路径填入 `config.yaml` 的 `lark_cli.path`。

下面以 Codex CLI 为一条完整的后端准备路径；也可以按自己的环境选择 Hermes 或 Claude Code。Codex 的 [OpenAI 官方文档](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)提供 npm 安装方式。首次登录时按 CLI 提示完成授权：

```bash
npm install --prefix "$HOME/.local/share/feishu-shadow-codex" '@openai/codex@latest'
export PATH="$HOME/.local/share/feishu-shadow-codex/node_modules/.bin:$PATH"
codex --version
codex login
codex login status
```

账号、网络与模型访问需要在运行 daemon 的机器上可用；`doctor` 会检查选定后端的 CLI 和登录状态，但检查成功不代表实际模型调用一定成功。

## 2. 创建应用并授权

在飞书开放平台创建企业自建应用、启用机器人能力，并在应用中申请以下权限；按租户要求完成管理员审批、发布和安装。项目的 `doctor` 当前要求用户身份具有下面**全部** scope：

```text
im:chat:read
im:message
im:message.group_msg:get_as_user
im:message.p2p_msg:get_as_user
im:message.reactions:read
im:message.send_as_user
im:message:readonly
search:message
```

机器人身份发送消息需要应用开通 `im:message:send_as_bot`；如需下载消息图片或文件，还需应用的 `im:message:readonly`。具体权限是否可申请和是否已生效以飞书开放平台及 CLI 返回的权限错误为准。将机器人加入测试群；`bot_joined: true` 是策略声明，不会自动拉机器人入群。

在**交互式终端**绑定该应用，按提示输入 App ID；密钥通过 stdin 传入，避免写进 shell 历史或进程参数。将 `cli_xxx` 换成你自己的 App ID：

```bash
read -r -s -p 'Feishu App Secret: ' FEISHU_SETUP_SECRET
printf '\n'
printf '%s' "$FEISHU_SETUP_SECRET" | lark-cli config init \
  --app-id 'cli_xxx' --app-secret-stdin --brand feishu
unset FEISHU_SETUP_SECRET
```

如果 CLI 提示当前是 Hermes/OpenClaw Agent 工作区，请先阅读 `lark-cli config bind --help` 并按已有应用的绑定方式操作；本项目需要允许 user 身份读取会话。不要无意覆盖其他工作区的绑定。

用户授权还需由登录账号完成。以下命令显式限定为本项目检查的 scope；不要省略 `--scope`，因为无范围参数的 `auth login` 会请求全部已知业务域：

```bash
lark-cli auth login --scope 'im:chat:read im:message im:message.group_msg:get_as_user im:message.p2p_msg:get_as_user im:message.reactions:read im:message.send_as_user im:message:readonly search:message'
lark-cli auth status --verify --json
```

按命令提示在浏览器完成授权。检查输出中的 `identities.user.openId`（`ou_...`）、用户 scope、`identities.bot` 的可用状态和 bot open ID；不要把完整授权输出、链接或凭证粘贴到公开位置。缺少 user scope 时，先在应用后台开通，再对用户重新授权；bot 权限则在应用后台配置。

## 3. 配置 owner 和测试群

使用上一步得到的用户 open ID 作为 `owner.open_id`。查找你有权访问的测试群并记录其 `chat_id`（`oc_...`）：

```bash
lark-cli im +chat-search --as user --query '测试群名称' --json
```

群名可能不唯一，先核对结果。将 `config.yaml` 中示例的 `oc_xxx` 替换为真实群 ID，并修改相应字段，其他字段可先保持示例默认值：

```yaml
owner:
  open_id: ou_替换为自己的open_id
  name: Owner
chats:
  oc_替换为测试群chat_id:
    name: 测试群
    auto_reply: false
    bot_joined: true
    resource_download: true
tool_permissions: read_only
agent_backend:
  provider: codex
```

示例文件已经有这些顶层键，请在原位置修改，不要追加同名 YAML 块。若使用已有 Hermes/Claude Code，将 `agent_backend.provider` 设为相应值并先完成其安装与登录。

`auto_reply: false` 仍表示该群已配置、可以采集；它只关闭该群自动回复。项目不会读取未配置群。首次添加群策略后不会回填启用前的旧 `@` 消息。由于飞书查询时间可能只有秒级精度，系统保守地跳过策略启用所在的整秒；该秒内新发送的消息也可能不会触发自动处理。示例中的 `reply_policy.p2p_auto_reply` 默认为 `true`：进入生产模式后，满足 gate 的用户私聊可能自动回复；如不希望如此，请在导入前设为 `false`。

## 4. 初始化并检查预演环境

如果生产策略尚未初始化，显式从 YAML 初始化预演策略：

```bash
uv run --locked python -m feishu_shadow_agent policy import-config --config config.yaml --mode dry-run
uv run --locked python -m feishu_shadow_agent doctor --config config.yaml --mode dry-run
```

所有 critical 项应为 `OK`；失败时先按 `doctor` 的缺失 scope、CLI 路径或策略初始化提示修复。若生产库已经初始化，首次打开预演环境会先复制生产的当前策略；它不会复制消息、任务或待发送动作。之后两套策略独立，需要时显式运行：

```bash
uv run --locked python -m feishu_shadow_agent policy sync-production --config config.yaml --mode dry-run
```

同步会替换预演策略；预演中被移除群的未完成工作会暂停。要编辑预演策略，可在 Console 的预演模式中操作，或给 `policy` 命令加 `--mode dry-run`。

## 5. 在测试会话中预演

保持一个终端运行 daemon，另开终端启动 Console。两个终端都需要相同的 CLI `PATH` 和后端登录环境：

```bash
uv run --locked python -m feishu_shadow_agent daemon --config config.yaml --dry-run
```

```bash
uv run --locked python -m feishu_shadow_agent console --config config.yaml --mode dry-run
uv run --locked python -m feishu_shadow_agent status --config config.yaml --mode dry-run
```

通过 Console 输出的本地链接打开页面，确认界面标识为“本地预演”。请测试群的另一位成员向 owner 发一条新 `@` 消息，查看任务与 Agent 结果，以及按实际结果生成的审批或 dispatch preview。审批仅在该 Console 内处理，不发送 owner 通知、不读取机器人审批私聊、不接收卡片回调。预演会先完整枚举用户私聊并排除机器人会话；机器人身份或枚举不完整时，私聊采集暂停，群处理继续。

预演数据库、资源和日志默认分别为 `data/agent.sqlite3.dry-run`、`data/resources.dry-run`、`logs/agent.jsonl.dry-run`；若修改了配置路径，则在原文件或目录名后追加 `.dry-run`。项目自身不实际发送预演回复，但 `full_access` Agent 可独立运行 shell/飞书 CLI 并产生外部写入；dry-run 不限制这些工具，建议首次使用 `read_only` 或外部沙箱。

## 6. 切换生产

核对测试结果和策略后，再单独初始化并检查生产状态。生产 Console 默认不加 `--mode dry-run`：

```bash
uv run --locked python -m feishu_shadow_agent policy import-config --config config.yaml
uv run --locked python -m feishu_shadow_agent doctor --config config.yaml
uv run --locked python -m feishu_shadow_agent daemon --config config.yaml
```

生产发送使用生产运行新生成的审批和动作，预演记录不会被提升为生产发送。升级旧版时，旧生产数据库可能包含以前混存的预演记录；本项目不会自动删除它。若确定不保留旧数据，先停止对应 daemon 和 Console，再由操作者按实际配置路径处理旧库及其 SQLite 辅助文件。
