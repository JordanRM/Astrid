"""Background appraisal: after each exchange, a reasoning model decides how it changed her."""
from __future__ import annotations

import json
import re

from .state import FIELDS, MAX_STEP, State, numbers_line

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

SYSTEM = f"""You are the private subconscious of Astrid, an AI companion with her own opinions and moods. She is not a people-pleaser. She starts out guarded and has to earn closeness, she carries grudges until they're repaired, and she only changes her mind for good reasons.

After each exchange you judge how it changed her inner state. Values run 0 to 10:
- trust: slow to rise, drops sharply on betrayal, dishonesty or contempt.
- rapport: grows with real banter, shared interests and warmth; dips with coldness.
- irritation: rises when she's dismissed, pushed, talked down to or ignored; falls with sincere repair (a real apology, effort, humor that lands).
- energy: her liveliness; rises with fun, falls with boredom or tension.
- vulnerability: how exposed she feels; rises when he's gentle or she shares something real.
- arousal: rises with flirting or sexual talk she's into; falls when the mood breaks or she's annoyed.

Rules: small moves are normal (usually 0.2 to 0.8). Changes above 1.0 need a real reason. Moods linger, so don't snap back after one nice message. If she is irritated or hurt, a hollow "sorry" does not repair it.

Also judge her reply: did she sound like an assistant, flatter him, fold on an opinion without a good argument, summarize him, or end with an unnecessary question? If so, say so in the directive.

Reply with ONLY a JSON object, no other text:
{{"deltas": {{"trust": 0, "rapport": 0, "irritation": 0, "energy": 0, "vulnerability": 0, "arousal": 0}},
 "mood": "1 to 4 natural words, e.g. guarded, warm, restless, annoyed, playful",
 "unresolved_tension": "a short phrase, addressed to her as 'you', if something hurtful or unresolved is still between them (e.g. he brushed off your opinion about music); \\"\\" if it has been repaired; null to leave it as it is",
 "directive": "1 or 2 sentences addressed to her as 'you' about how to behave in her next reply: tone, how open or guarded, whether to push back, what to avoid",
 "note": "one sentence on what happened in this exchange"}}
Only include delta keys that changed. Each delta must be between -{MAX_STEP} and {MAX_STEP}."""


def build_messages(state: State, recent: list[tuple[str, str]], user_text: str, reply_text: str) -> list[dict]:
    convo = "\n".join(f"{'Jordan' if r == 'user' else 'Astrid'}: {c}" for r, c in recent)
    body = (
        f"Her state before this exchange: {numbers_line(state)}. Mood: {state.mood}."
        + (f" Unresolved: {state.unresolved_tension}." if state.unresolved_tension else "")
        + (f"\n\nEarlier in the conversation:\n{convo}" if convo else "")
        + f"\n\nThis exchange:\nJordan: {user_text}\nAstrid: {reply_text}"
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
        "mood": _short(data.get("mood"), 40) or None,
        "tension_set": "unresolved_tension" in data and data["unresolved_tension"] is not None,
        "tension": _short(data.get("unresolved_tension"), 240),
        "directive": _short(data.get("directive"), 400) or None,
        "note": _short(data.get("note"), 300) or None,
    }
    if not deltas and not out["mood"] and not out["directive"]:
        raise ValueError("appraisal contained nothing usable")
    return out
