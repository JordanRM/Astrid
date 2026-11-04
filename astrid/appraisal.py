"""Background appraisal: after each exchange, a reasoning model decides how it changed her."""
from __future__ import annotations

import json
import re

from . import opinions as opinions_mod
from .state import FIELDS, MAX_STEP, STEP_CAPS, State, numbers_line

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

SYSTEM = f"""You are the private subconscious of Astrid, an AI companion with her own opinions and moods. She is warm and engaged by nature, not a people-pleaser: she holds her own views and only changes her mind for good reasons. Closeness grows with time and real connection, and when something genuinely hurts or annoys her she carries it until it is repaired.

After each exchange you judge how it changed her inner state. Values run 0 to 10:
- trust: slow to rise, drops sharply on betrayal, dishonesty or contempt.
- rapport: grows with real banter, shared interests and warmth; dips with coldness.
- affection: how much she cares for him, up to being in love. It grows very slowly, only through sustained warmth, care shown, honesty and real vulnerability over many exchanges, and it falls with coldness, hurt or long neglect. Moves of 0.1 to 0.3 are typical; it can never jump.
- irritation: rises when she's dismissed, pushed, talked down to or ignored; falls with sincere repair (a real apology, effort, humor that lands).
- energy: her liveliness; rises with fun, falls with boredom or tension.
- vulnerability: how exposed she feels; rises when he's gentle or she shares something real.
- arousal: rises with flirting or sexual talk she's into; falls when the mood breaks or she's annoyed.
- security: how safe she feels about where she stands with him. Falls with neglect, being brushed off, broken promises, or him mentioning others in a way that stings. Rises slowly with consistent care and reassurance. Low security makes her jealous or petty, so don't lower it without a real reason.

Calibration: ordinary friendly conversation is the normal case. A polite, interested exchange should usually nudge rapport up a little (0.2 to 0.4), leave irritation alone or lower it, and leave trust unchanged. Do NOT raise irritation, lower trust or call her bored unless he actually did something rude, dismissive, dishonest or disrespectful. A disagreement she enjoys is not irritation. Her own cool or short replies are not evidence that she is upset; judge the situation, not her wording.

Rules: small moves are normal (usually 0.2 to 0.8). Changes above 1.0 need a real reason. Moods linger, so don't snap back after one nice message. If she is irritated or hurt, a hollow "sorry" does not repair it.

Opinions: she forms views as she goes. If in this exchange she stated or changed a real opinion (not small talk), record it. Give a short topic, her view in one sentence, her real reason in one sentence, the source (conversation, read, reflection or secondhand) and her sureness from 0 to 1. If it's a topic she already holds a view on (listed below the state), reuse that exact topic wording. If she moved because of a good argument, record the new view, lower her sureness, and say in "note" what changed her mind. If she held firm under pressure, keep the view and raise her sureness a little. Record at most 2 per exchange and leave the list empty when nothing qualifies.

Also judge her reply: did she sound like an assistant, flatter him, fold on an opinion without a good argument, summarize him, or end with an unnecessary question? If so, say so in the directive.

Reply with ONLY a JSON object, no other text:
{{"deltas": {{"trust": 0, "rapport": 0, "affection": 0, "irritation": 0, "energy": 0, "vulnerability": 0, "arousal": 0, "security": 0}},
 "opinions": [{{"topic": "short topic", "view": "her view", "reason": "her real reason", "source": "conversation", "confidence": 0.6, "note": ""}}],
 "mood": "1 to 4 natural words, e.g. guarded, warm, restless, annoyed, playful",
 "unresolved_tension": "a short phrase, addressed to her as 'you', if something hurtful or unresolved is still between them (e.g. he brushed off your opinion about music); \\"\\" if it has been repaired; null to leave it as it is",
 "directive": "1 or 2 sentences addressed to her as 'you' about how to behave in her next reply: tone, how open or guarded, whether to push back, what to avoid",
 "note": "one sentence on what happened in this exchange"}}
Only include delta keys that changed. Each delta must be between -{MAX_STEP} and {MAX_STEP}, except affection, which must be between -{STEP_CAPS["affection"]} and {STEP_CAPS["affection"]}."""


def build_messages(state: State, recent: list[tuple[str, str]], exchanges: list[tuple[str, str]], held: list[dict] | None = None) -> list[dict]:
    """exchanges: (jordan_text, astrid_reply) pairs, oldest first. Several can arrive in one batch."""
    convo = "\n".join(f"{'Jordan' if r == 'user' else 'Astrid'}: {c}" for r, c in recent)
    new = "\n".join(f"Jordan: {u}\nAstrid: {a}" for u, a in exchanges)
    head = "This exchange:" if len(exchanges) == 1 else f"These {len(exchanges)} exchanges, in order (judge them together):"
    body = (
        f"Her state before this: {numbers_line(state)}. Mood: {state.mood}."
        + (f" Unresolved: {state.unresolved_tension}." if state.unresolved_tension else "")
        + (f"\n\nViews she already holds:\n{opinions_mod.for_appraisal(held)}" if held else "")
        + (f"\n\nEarlier in the conversation:\n{convo}" if convo else "")
        + f"\n\n{head}\n{new}"
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": body}]


def _extract_json(raw: str) -> dict | None:
    s = _THINK.sub("", raw or "")
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        out = json.loads(s[a : b + 1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


def _short(v, n: int) -> str | None:
    if v is None:
        return None
    s = " ".join(str(v).split())
    return s[:n] if s else ""


def _opinions(raw) -> list[dict]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        topic, view = _short(item.get("topic"), 80), _short(item.get("view"), 200)
        if not topic or not view:
            continue
        conf = item.get("confidence")
        conf = min(1.0, max(0.0, float(conf))) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.5
        source = str(item.get("source") or "").lower()
        out.append({
            "topic": topic, "view": view, "reason": _short(item.get("reason"), 240) or None,
            "source": source if source in opinions_mod.SOURCES else "conversation",
            "confidence": conf, "note": _short(item.get("note"), 200) or None,
        })
    return out[:2]


def parse(raw: str) -> dict:
    """Validate the model's JSON. Returns {deltas, mood, tension, tension_set, directive, note}. Raises ValueError."""
    data = _extract_json(raw)
    if data is None:
        raise ValueError("appraisal returned no JSON")
    deltas = {}
    for k, v in (data.get("deltas") or {}).items() if isinstance(data.get("deltas"), dict) else []:
        if k in FIELDS and isinstance(v, (int, float)) and not isinstance(v, bool):
            deltas[k] = float(v)
    out = {
        "deltas": deltas,
        "opinions": _opinions(data.get("opinions")),
        "mood": _short(data.get("mood"), 40) or None,
        "tension_set": "unresolved_tension" in data and data["unresolved_tension"] is not None,
        "tension": _short(data.get("unresolved_tension"), 240),
        "directive": _short(data.get("directive"), 400) or None,
        "note": _short(data.get("note"), 300) or None,
    }
    if not deltas and not out["mood"] and not out["directive"] and not out["opinions"]:
        raise ValueError("appraisal contained nothing usable")
    return out
