# Separate Message Acquisition from Message Eligibility

Status: accepted

Feishu Shadow Agent separates Message Acquisition from Message Eligibility. Acquisition records which raw messages source-specific Lark operations make available, including active-watch task and watch-key matching; after normalization, a shared source-aware eligibility policy decides whether each acquired message may proceed to task routing without selecting a target task. This replaces the earlier evaluation boundary that equated `kept` with “handed to `process_raw_message`”, which mixed Lark retrieval mechanics with message value and could incorrectly discard meaningful owner interventions or count deterministic router suppression as acquisition behavior.

## Consequences

Production and evaluation must call the same Message Eligibility policy. The policy receives a normalized message plus its acquisition sources and must not read task/store state. It may reject loops, `@All`, and irrelevant noise, but must preserve owner interventions and valid active-watch follow-ups. Task ownership remains exclusively a router decision, active-watch lifecycle remains an Acquisition concern, and Message Acquisition remains observable separately from eligibility.

Ingress replay uses a complete prior run directory rather than a raw-message file. Lark-owned `group_at_me` observations remain frozen because server-side search cannot be reproduced offline; local active-watch acquisition is rerun from explicit task/watch-key fixtures, followed by the current Message Eligibility policy. Changes to Lark query behavior therefore require a new live run, while local acquisition and eligibility changes remain replayable.
