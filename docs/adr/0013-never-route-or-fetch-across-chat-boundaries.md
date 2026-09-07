# Never route or fetch across chat boundaries

Status: accepted

Reply targets, thread, sender, and mention shortcuts match tasks only within the message's own chat, and content that originates in another chat — typically carried in by a Merged Forward — is context text of the current chat only. A Cross-Chat Reference is never a routing signal, and the daemon never fetches origin-chat messages, state, or resources. This keeps task ownership, context, and operator visibility scoped to one chat, which is the safety boundary the owner relies on; enabling cross-chat matching was considered and rejected because it would require per-cross-chat authorization and visibility rules that do not exist today. Child resources inside a Merged Forward remain undownloadable placeholders until a future acquisition path exposes child message ids; that is an external lark-cli dependency, not a routing change.
