# Bot membership is derived at runtime, never auto-mutated in the Product Policy Store

Status: accepted

Whether the bot identity is a member of a chat is a runtime fact, not an owner-configured truth: it is established by send/download failure attribution and active membership probes, and layered into Effective Policy as a Bot Membership Fact. A confirmed absence degrades replies according to reply_identity and allow_user_fallback, blocks resource downloads, and notifies the owner; the daemon never writes `bot_joined` into the Product Policy Store and never produces a Policy Audit for it, because only the owner is a legitimate policy actor. Auto-mutating policy on runtime observation was considered and rejected: it would blur the Policy Audit actor semantics and silently change owner-authored policy. Resolution stays manual — the owner re-adds the bot or adjusts policy — because automatically re-joining chats is an intrusive action requiring separate authorization.

The daemon probes candidate group chats with the installed `lark-cli 1.0.56`
contract `im chat.members bots --chat-id <id> --as user --json`. The response
contains bot entries identified by `bot_id`; a successful list can therefore
confirm presence or absence. Authentication failure, permission failure,
malformed output, and probe exceptions produce `unknown`, never `absent`.

Facts are stored as runtime checkpoints, not policy rows. They record the
observed status, check time, next probe time, source, error, absence episode,
and last confirmed status. Confirmed observations are authoritative for 300
seconds by default; unknown probes retry after 60 seconds. An expired fact is
read as unknown until refreshed. The last confirmed status prevents a failed
probe between two absence observations from creating another absence alert.
Initial presence is quiet; each newly confirmed absence creates at most one
owner notification, and the next confirmed recovery creates one recovery
notification.

Effective Policy layers a fresh runtime fact over the owner-authored
`bot_joined` value for group chats. `present` forces the effective capability
on, `absent` forces it off, and `unknown` or an unobserved chat preserves the
configured value. A bot-preferred reply may fall back to the user identity only
when `allow_user_fallback` permits it. A forced bot reply and bot-dependent
resource downloads remain blocked while absence is confirmed. Bot-identity
resource-download and message-reply failures refresh absence only when their
JSON error envelope has the documented `10002` code (bot not in the chat).
Authentication (`234002`), visibility (`234040`), scope, resource ownership,
plain-text errors, and malformed error output remain ordinary failures and do
not alter the membership fact. Confirmed absence uses the same episode-level
notification deduplication.
