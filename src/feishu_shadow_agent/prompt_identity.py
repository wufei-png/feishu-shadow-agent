from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

PROMPT_VERSIONS: Final[dict[str, str]] = {
    "router": "v1",
    "task_session": "v1",
    "reply_postprocess": "v1",
    "owner_style_refresh": "v1",
    "ingress_judge": "v1",
    "semantic_judge": "v1",
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
