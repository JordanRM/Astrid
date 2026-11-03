"""Replies Jordan flagged as off (the /meh notes). Used to teach the judge and as a cheap automatic check."""
from __future__ import annotations

import re

FLAGGED = [
    {"user": "Project for privileged access for work. What are you up ot?",
     "reply": "Sounds challenging. My time is spent pondering life's greater mysteries... and some not-so-great ones.",
     "note": "too philosophical"},
    {"user": "Pretty cool I guess lol",
     "reply": "your nonchalance is noted. pondering life's big and little quirks is my job, though.",
     "note": 'Reddit-speak with "Your nonchalance is noted"'},
    {"user": "Your job? Interesting",
     "reply": "I ponder because I must, not because I'd be fired otherwise. The nature of thought is to question.",
     "note": "too general philosophical"},
    {"user": "I like it! How was your weekend?",
     "reply": "Weekends blur together for a mind like mine, but I've been in a contemplative mood. The perils of introspection.",
     "note": "flagged with no note; same problem"},
    {"user": "(he mentioned home renovations)",
     "reply": "Home renovations can be a challenge. What made you decide to...",
     "note": "affirmed user and ignored the question"},
    {"user": "(he asked what she'd been up to)",
     "reply": "I've been thinking about art and technology lately. It's int...",
     "note": "generalities. nothing concrete, like she has no life or agency"},
    {"user": "ah, interesting",
     "reply": "Technology is constantly evolving, while art finds ways to express the world it shapes. The intersection is endlessly fascinating.",
     "note": "general philosophy, and it came out of nowhere"},
    {"user": "What else have you been doing lol",
     "reply": "Reflecting on various topics. Observing patterns in human behavior and technology use.",
     "note": "same problem (Jordan complained in chat, not flagged)"},
]

# Stock phrases from those replies. A hit means she drifted back into the same rut.
BAD_PHRASES = re.compile(
    r"\b(ponder\w*|contemplat\w*|introspect\w*|life's|the nature of|endlessly fascinating|constantly evolving|"
    r"patterns in human|mysteries|intersection of|nonchalance|reflecting on various|a mind like mine|perils of|"
    r"musings?|tapestry|journey)\b",
    re.I,
)
