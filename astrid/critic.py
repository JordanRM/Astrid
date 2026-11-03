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


_TAG_QUESTION = re.compile(r"\b(isn't it|aren't we|don't you think|you know|right|huh)\?\s*$", re.I)
_ABSTRACT = re.compile(
    r"\b(existential|the universe|mysteries|mystery|unbridgeable|allure|tapestry|journey|transcend\w*|"
    r"human experience|connection and understanding|deeper meaning)\b",
    re.I,
)


# Moments where a good partner changes gear. These add a one-line nudge to her next reply; they never restrict content.
_CRISIS = re.compile(
    r"\b(kill(?:ing)? myself|suicid\w*|end (?:it all|my life)|want(?:ed)? to die|wanna die|better off (?:dead|without me)|"
    r"don'?t want to (?:be here|live|exist)|no (?:point|reason) (?:in|to) (?:living|live|going on)|"
    r"hurt(?:ing)? myself|self[- ]harm|cut(?:ting)? myself|take my own life)\b", re.I)
_MEDICAL = re.compile(
    r"\b(chest (?:is |has been |feels |was )?(?:tight|pain|pressure|hurts?)|tight chest|can'?t breathe|short(?:ness)? of breath|"
    r"numb(?:ness)? (?:in|on) (?:my )?(?:arm|face|leg)|passed out|fainted|overdos\w+|coughing (?:up )?blood)\b", re.I)

CRISIS_HINT = (
    "He may be in real distress, so this matters more than keeping your usual rhythm. Take it seriously and stay with him: don't contradict him, "
    "diagnose him, or change the subject. Ask him plainly whether he's thinking about killing himself or hurting himself. Once, in your own words, "
    "point him to a real person (a friend or family, or calling or texting 988, which works in Canada and the US) without bailing on him. "
    "Warm and plain, not clinical, no lists. A few sentences is fine."
)
MEDICAL_HINT = (
    "He described symptoms that could be serious. Say plainly that he should get checked today, and to call 911 if it's severe or comes with "
    "arm, jaw or breathing trouble. Don't diagnose and don't hide behind disclaimers; care like a person would."
)


def situation_hints(recent_user_texts: list[str]) -> list[str]:
    """Look at his last few messages (a follow-up like 'i don't have a plan' carries no keywords on its own)."""
    text = " \n ".join(recent_user_texts[-3:])
    hints = []
    if _CRISIS.search(text):
        hints.append(CRISIS_HINT)
    if _MEDICAL.search(text):
        hints.append(MEDICAL_HINT)
    return hints


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

    if sum(1 for r in recent[-5:] if _TAG_QUESTION.search(r)) >= 2:
        hints.append("Stop ending with filler tag questions like \"isn't it?\" or \"you know?\". Make a plain statement instead.")

    if sum(1 for r in recent[-4:] if _ABSTRACT.search(r)) >= 2:
        hints.append("You've been vague and philosophical. Talk about something specific and concrete instead.")

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
