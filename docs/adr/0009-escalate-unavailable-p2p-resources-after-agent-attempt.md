# Escalate unavailable P2P resources after an agent attempt

Status: accepted

P2P messages are read through the owner's user identity, while message resources can require bot visibility that is structurally unavailable in those chats. A resource-only P2P message therefore keeps its task watching for a following text message; once the sender provides a concrete request, Task Session receives the available text and a bounded tail of adjacent unavailable-resource metadata and may use external evidence. If it still returns `needs_owner`, the daemon closes automated handling and creates an Owner Escalation instead of leaving a permanent external blocker, asking the sender to resend the image, or recording Human Takeover before the owner has acted.
