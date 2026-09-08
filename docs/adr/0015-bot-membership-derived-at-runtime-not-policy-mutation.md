# Bot membership is derived at runtime, never auto-mutated in the Product Policy Store

Status: accepted

Whether the bot identity is a member of a chat is a runtime fact, not an owner-configured truth: it is established by send/download failure attribution and active membership probes, and layered into Effective Policy as a Bot Membership Fact. A confirmed absence degrades replies according to reply_identity and allow_user_fallback, blocks resource downloads, and notifies the owner; the daemon never writes `bot_joined` into the Product Policy Store and never produces a Policy Audit for it, because only the owner is a legitimate policy actor. Auto-mutating policy on runtime observation was considered and rejected: it would blur the Policy Audit actor semantics and silently change owner-authored policy. Resolution stays manual — the owner re-adds the bot or adjusts policy — because automatically re-joining chats is an intrusive action requiring separate authorization.
