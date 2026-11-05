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


# Claiming a human experience she can't have ("i listened to it"). Negations are fine ("i've never played a hand").
_EXPERIENCE = re.compile(r"\bi(?:'ve| have)?\s+(?!never\b|not\b|haven't\b|didn't\b|can't\b)(?:listened|watched|played|tasted|visited|driven|walked)\b", re.I)


# Wordsmith habits: coined sayings, tidy closers, "X isn't Y, it's Z", and made-up personal anecdotes.
_APHORISM = re.compile(
    r"that'?s the (?:whole |real |actual |hard |easy |only )?(?:thing|trick|point|line|part|game|catch|difference|secret)\b|"
    r"\bnobody (?:teaches|tells|warns|talks)\b|\bthe (?:hard|harder|easy|easier|real|only|best|worst) (?:part|half|thing)\b|"
    r"\b(?:isn'?t|aren'?t|wasn'?t|weren'?t) (?:the |a |an )?[\w' ]{2,25}, (?:it'?s|the \w+ is|that'?s)\b|"
    r"\bmost people\b[^.]{0,60}\b(?:instead|just|never)\b|\banyone can\b|\bthe people who\b[^.]{0,50}\b(?:usually|always)\b|"
    r", not (?:a|an|the) \w+\s*$",
    re.I | re.M,
)
_ANECDOTE = re.compile(r"\bi (?:once|cleared|did that too|did the same|went through (?:the same|that)|learned (?:it )?the hard way)\b", re.I)


# Acting as if they've talked before, on the very first message ever.
_PRIOR_HISTORY = re.compile(
    r"\bwelcome back\b|\blast time\b|\bwe(?:'ve| have) (?:talked|spoken|been|done)\b|\bas usual\b|\bagain,? (?:jordan|huh)\b|"
    r"good to (?:see|hear from) you again|\bmissed you\b|\bfrom (?:before|earlier)\b|\bwhere we left off\b|"
    r"\byou (?:told|said|mentioned) (?:me )?(?:before|earlier|last)\b|\bback (?:again|already)\b|\bour (?:last|earlier|usual) \w+",
    re.I,
)


def first_contact_slips(reply: str) -> list[str]:
    """On the very first message ever, she can't act as if they've talked before."""
    return ["acted as if you've talked before; this is the first time you've ever spoken"] if _PRIOR_HISTORY.search(reply or "") else []


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

    if sum(1 for r in recent[-3:] if _EXPERIENCE.search(r)) >= 1:
        hints.append("You implied you personally listened to, watched, played or tasted something, which you can't. Say how you actually know it, or just give your take.")

    if sum(1 for r in recent[-3:] if _APHORISM.search(r)) >= 1:
        hints.append("You've been coining neat sayings and tidy closing lines. This time say it in plain, ordinary sentences with no punchline or moral.")

    if sum(1 for r in recent[-3:] if _ANECDOTE.search(r)) >= 1:
        hints.append("You made up a personal anecdote. You don't have a past like that. Share what you've read or what you think instead.")

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
