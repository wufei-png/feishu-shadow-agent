# Lark/Feishu CLI：最近2天消息 + 前后 N 条上下文

以下默认用 `lark-cli`，如你本地是 `lark` 请把命令前缀替换。

## 0. 统一时间变量（最近2天）
```bash
START_TIME="$(date -v -2d '+%Y-%m-%dT00:00:00%:z')"
END_TIME="$(date '+%Y-%m-%dT23:59:59%:z')"
# 示例：2026-07-07T00:00:00+08:00
```

## 1) 最近2天被 @ 我 的消息（用户身份）
```bash
lark-cli im +messages-search \
  --as user \
  --is-at-me \
  --start "$START_TIME" \
  --end "$END_TIME" \
  --page-all \
  --format json \
  --no-reactions \
  > at_me_last_2days.json
```

## 2) 最近2天的私聊消息（P2P）
```bash
lark-cli im +messages-search \
  --as user \
  --chat-type p2p \
  --start "$START_TIME" \
  --end "$END_TIME" \
  --page-all \
  --format json \
  --no-reactions \
  > p2p_last_2days.json
```

### 说明
- `--is-at-me`：只要 @ 我。
- `--chat-type p2p`：只要私聊。
- `+messages-search` 输出的结果通常在 `json.messages[]` 里会含 `chat_id`、`message_id`、`create_time`。

---

## 3) 对“每一条种子消息”补拉前后 N 条（建议先确认字段是否完整）

### 3.1 取前后 N 条的单条命令（按消息时间窗 + 排序）
假设种子消息信息是：
- `CHAT_ID`：消息所在会话 ID
- `TS`：消息时间（`2026-07-09T10:00:00+08:00`）
- `N`：前后条数，例如 5

```bash
# 前 N 条：向前（越早）
lark-cli im +chat-messages-list \
  --as user \
  --chat-id "$CHAT_ID" \
  --end "$TS" \
  --order desc \
  --page-size "$N" \
  --format json > context_before_${N}_$TS.json

# 后 N 条：向后（越新）
lark-cli im +chat-messages-list \
  --as user \
  --chat-id "$CHAT_ID" \
  --start "$TS" \
  --order asc \
  --page-size "$N" \
  --format json > context_after_${N}_$TS.json
```

> 上下文里可能会包含种子消息本身（取决于时间边界和接口特性），后续可按 `message_id` 去重。

### 3.2 批量脚本：把“命令1/2的结果”各自展开为前后 N 条
下面脚本把 `seed.json` 里的每条消息拉上下文，并按 `chat_id, message_id` 去重。

```bash
seed_json="/path/to/seed.json"   # 换成 at_me_last_2days.json 或 p2p_last_2days.json
N=5
OUT_DIR="./lark-message-context"
mkdir -p "$OUT_DIR"

jq -r '.messages[] | [ (.chat_id // empty), (.message_id // .meta_data.message_id // empty), (.create_time // empty) ] | @tsv' "$seed_json" |
while IFS=$'\t' read -r CHAT_ID MSG_ID TS; do
  [ -z "$CHAT_ID" ] && continue
  [ -z "$TS" ] && continue

  # 先清理斜杠等非法文件名字符
  SAFE_TS="$(printf '%s' "$TS" | tr -cs 'A-Za-z0-9._-+' '_')"

  lark-cli im +chat-messages-list --as user --chat-id "$CHAT_ID" --order desc --end "$TS" --page-size "$N" --format json \
    > "$OUT_DIR/${MSG_ID:-${CHAT_ID}_before}_${SAFE_TS}_before.json"

  lark-cli im +chat-messages-list --as user --chat-id "$CHAT_ID" --order asc --start "$TS" --page-size "$N" --format json \
    > "$OUT_DIR/${MSG_ID:-${CHAT_ID}_after}_${SAFE_TS}_after.json"

done
```

### 3.3 可选：合并为“最终上下文文件”
```bash
jq -s 'flatten' "$OUT_DIR"/*_before.json "$OUT_DIR"/*_after.json > context_merged.json
```

---

## 4) 关键理解
- `+messages-search` 负责“筛选命令（@我/私聊）”。
- `+chat-messages-list` 支持 `start/end + order/page-size`，用于“按时间窗”补齐前后上下文。
- 这个CLI里没有“直接按 message_id 自动前后各N条”的单步参数；可用时间窗法实现（精度通常够用，但同秒多条消息时会有边界重叠）。

