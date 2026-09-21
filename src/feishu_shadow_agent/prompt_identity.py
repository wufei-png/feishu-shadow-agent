from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

PROMPT_VERSIONS: Final[dict[str, str]] = {
    "router": "v2",
    "task_session": "v2",
    "reply_postprocess": "v2",
    "owner_style_refresh": "v2",
    "ingress_judge": "v2",
    "semantic_judge": "v2",
    "structured_output": "v1",
}


@dataclass(frozen=True)
class PromptIdentity:
    kind: str
    version: str
    sha256: str


def identify_prompt(kind: str, prompt: str) -> PromptIdentity:
    try:
        version = PROMPT_VERSIONS[kind]
    except KeyError as exc:
        raise ValueError(f"unknown prompt kind: {kind}") from exc
    return PromptIdentity(
        kind=kind,
        version=version,
        sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )


__all__ = ["PROMPT_VERSIONS", "PromptIdentity", "identify_prompt"]
