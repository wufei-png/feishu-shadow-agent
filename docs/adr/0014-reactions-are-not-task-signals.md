# Reactions are not task signals

Status: accepted

A Pure Reaction — an emoji-only action on a Feishu message — never triggers, continues, or extends a Task Session. Message acquisition always runs with reactions disabled, so reaction data never enters the pipeline, and a reaction is not a follow-up signal regardless of who reacts or on which message. Treating reactions as lightweight commands or acknowledgement signals was considered; it would require new reaction acquisition plus defined semantics for watch extension and approval, none of which the product needs today. If that changes, the feature must be added explicitly rather than inferred from reaction presence.
