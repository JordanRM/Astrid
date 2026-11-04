"""Views she has formed. They are saved with a reason and a source, and the relevant ones go back into her prompt."""
from __future__ import annotations

import re

SOURCES = ("conversation", "read", "reflection", "secondhand")
_STOP = {"that", "this", "with", "have", "what", "when", "your", "about", "just", "like", "they", "them", "then", "than",
         "there", "their", "really", "think", "know", "would", "could", "should", "from", "into", "more", "some", "very", "much"}


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z']{4,}", (text or "").lower()) if w not in _STOP}


def relevant(opinions: list[dict], texts: list[str], limit: int = 6) -> list[dict]:
    """Opinions whose topic or view shares words with the recent conversation, best match first."""
    query = set()
    for t in texts:
        query |= _words(t)
    scored = []
    for o in opinions:
        hits = len(query & _words(o["topic"] + " " + o["view"]))
        if hits:
            scored.append((hits, o.get("updated_at") or 0, o))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [o for _, _, o in scored[:limit]]


def render(opinions: list[dict]) -> str | None:
    if not opinions:
        return None
    lines = [
        "Views you've already formed on things that may be coming up. Stay consistent with them unless he gives you a real reason "
        "to change (a good argument or new information, not pressure or repetition). If he asks why, give your real reason."
    ]
    for o in opinions:
        why = f" Why: {o['reason']}" if o.get("reason") else ""
        lines.append(f"- {o['topic']}: {o['view']}.{why} (sureness {float(o['confidence']):.1f} out of 1, from {o['source']})")
    return "\n".join(lines)


def for_appraisal(opinions: list[dict], limit: int = 30) -> str:
    return "\n".join(f"- {o['topic']}: {o['view']} (sureness {float(o['confidence']):.1f})" for o in opinions[:limit])
