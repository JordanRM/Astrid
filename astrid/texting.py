"""Turns raw model output into how she actually texts: lowercase, no dashes, split into separate texts."""
from __future__ import annotations

import re

MAX_TEXTS = 4
_PREFIX = re.compile(r"^\s*(?:astrid|you)\s*:\s*", re.I)


_URL = re.compile(r"(https?://[^\s<>\"')\]]+)", re.I)


def _plain(text: str) -> str:
    text = re.sub(r"\s*//\s*", "\n\n", text)             # the example bank's "two texts" marker, if she copies it
    text = re.sub(r"\s*[—–]\s*", ", ", text)     # em and en dashes
    return text.lower()


def normalize(reply: str) -> str:
    text = _PREFIX.sub("", reply.strip())
    # Links are left exactly as they are: no lowercasing (paths are case-sensitive) and no splitting at the "//".
    text = "".join(part if i % 2 else _plain(part) for i, part in enumerate(_URL.split(text)))
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_texts(reply: str) -> list[str]:
    """Blank lines mean separate messages. More than MAX_TEXTS get folded into the last one."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", reply) if p.strip()]
    if len(parts) > MAX_TEXTS:
        parts = parts[: MAX_TEXTS - 1] + [" ".join(parts[MAX_TEXTS - 1:])]
    return parts or [reply.strip()]
