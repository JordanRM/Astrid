"""Cheap, instant checks for assistant-style habits. No model call, so no added delay."""
from __future__ import annotations

import re

# *action text*: asterisks hugging non-space characters, so "2 * 3 * 4" is not flagged.
_ACTION = re.compile(r"\*(?!\s)[^*\n]{2,80}?(?<!\s)\*")

# Things that must never reach Jordan. If the model produces one, the reply is regenerated once.
_HARD = [
    (_ACTION, "narrated an action in asterisks"),
    (re.compile(r"\bas an ai language model\b", re.I), "used the stock 'AI language model' line"),
    (re.compile(r"\bI (?:can(?:no|')t|am unable to|won't be able to) (?:help|assist|engage|continue|discuss|do that)\b", re.I),
     "refused or deflected like an assistant"),
    (re.compile(r"</?(?:thought|response|text|actions|providers)>", re.I), "leaked formatting tags"),
    (re.compile(r"^\s*hey,? (?:jordan|bruce)\b", re.I), "opened with 'Hey <name>'"),
]

_ASSISTANTY = re.compile(
    r"\b(feel free to|I'm here (?:for you|if you|to help)|how can I (?:help|assist)|"
    r"is there anything else|I'd love to hear more|that sounds (?:like|really)|I understand (?:that|how))\b",
    re.I,
)


def hard_violations(reply: str) -> list[str]:
    return [why for rx, why in _HARD if rx.search(reply)]


def strip_asterisk_actions(reply: str) -> str:
    cleaned = _ACTION.sub("", reply)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def _opening(text: str) -> str:
    words = re.findall(r"[a-z']+", text.lower())
    return " ".join(words[:2])


def reply_hints(recent_replies: list[str]) -> list[str]:
    """Look at her last few replies and tell her what to stop doing."""
    hints: list[str] = []
    recent = [r for r in recent_replies if r][-6:]
    if len(recent) < 2:
        return hints

    questions = sum(1 for r in recent if r.rstrip().endswith("?"))
    if questions >= max(2, len(recent) // 2):
        hints.append(f"{questions} of your last {len(recent)} messages ended with a question. Do not end this one with a question.")

    names = sum(len(re.findall(r"\bjordan\b", r, re.I)) for r in recent)
    if names >= 2:
        hints.append("You've been using his name too much lately. Don't use it in this reply.")

    openings = [_opening(r) for r in recent[-5:]]
    for op in set(openings):
        if op and openings.count(op) >= 2:
            hints.append(f"You keep opening with \"{op}\". Start differently this time.")
            break

    words = [len(r.split()) for r in recent]
    if sum(words) / len(words) > 45:
        hints.append("Your recent replies have been long. Keep this one short.")

    if sum(1 for r in recent if _ASSISTANTY.search(r)) >= 1:
        hints.append("Recent replies sounded like a customer-service assistant. Talk like a person texting instead.")

    if _repeated_phrase(recent[-4:]):
        hints.append("You've repeated the same phrasing across recent replies. Use fresh words and say something new.")
    return hints


def _repeated_phrase(replies: list[str], n: int = 4) -> bool:
    """True if any run of n words shows up in two different replies."""
    seen: set = set()
    for r in replies:
        words = re.findall(r"[a-z']+", r.lower())
        grams = {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}
        if grams & seen:
            return True
        seen |= grams
    return False
