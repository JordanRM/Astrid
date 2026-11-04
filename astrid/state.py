"""Her inner state: numbers that persist, drift back toward baseline, and shape her tone."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

FIELDS = ("trust", "rapport", "affection", "irritation", "energy", "vulnerability", "arousal", "security")

# Where each value settles when nothing is happening (fresh relationship: guarded, not hostile).
BASELINE = {
    "trust": 4.0,
    "rapport": 3.0,
    "affection": 1.0,
    "irritation": 1.0,
    "energy": 6.0,
    "vulnerability": 3.0,
    "arousal": 1.0,
    "security": 6.0,
}

# Hours for the gap between a value and its baseline to halve. Irritation and arousal cool
# within a day; trust and rapport only move slowly, so a fight still shows hours later.
HALF_LIFE_HOURS = {
    "trust": 24 * 30,
    "rapport": 24 * 30,
    "affection": 24 * 60,
    "irritation": 8,
    "energy": 12,
    "vulnerability": 12,
    "arousal": 4,
    "security": 24 * 14,
}

MAX_STEP = 1.5  # biggest change one exchange may cause in one field
STEP_CAPS = {"affection": 0.5, "security": 0.6}  # love grows slowly: it can never jump in a single exchange


@dataclass
class State:
    values: dict = field(default_factory=lambda: dict(BASELINE))
    mood: str = "neutral"
    unresolved_tension: str | None = None
    directive: str | None = None
    updated_at: float = 0.0

    def copy(self) -> "State":
        return State(dict(self.values), self.mood, self.unresolved_tension, self.directive, self.updated_at)


def clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, float(v)))


def decayed(state: State, now: float) -> State:
    """The state as it is now: each value has relaxed toward baseline since it was last saved."""
    out = state.copy()
    hours = max(0.0, (now - state.updated_at) / 3600.0) if state.updated_at else 0.0
    if hours:
        for k in FIELDS:
            base = BASELINE[k]
            gap = state.values[k] - base
            out.values[k] = base + gap * math.pow(0.5, hours / HALF_LIFE_HOURS[k])
    return out


def apply_deltas(state: State, deltas: dict, scale: float = 1.0) -> tuple[State, dict]:
    """Apply bounded changes. `scale` widens the caps when one appraisal covers several exchanges.
    Returns the new state and the deltas actually applied."""
    out = state.copy()
    applied = {}
    for k, d in (deltas or {}).items():
        if k not in FIELDS:
            continue
        try:
            d = float(d)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(d):
            continue
        cap = STEP_CAPS.get(k, MAX_STEP) * scale
        d = max(-cap, min(cap, d))
        new = clamp(out.values[k] + d)
        applied[k] = round(new - out.values[k], 3)
        out.values[k] = new
    return out, applied


def circadian_energy_shift(hour: int) -> float:
    """Late night and early morning pull energy down; evenings ease off a little."""
    if hour >= 23 or hour < 6:
        return -3.0
    if hour < 9:
        return -1.0
    if hour >= 21:
        return -1.0
    return 0.0


def _band(v: float, bands: list) -> str:
    for limit, text in bands:
        if v < limit:
            return text
    return bands[-1][1]


def render(state: State, local_now: datetime) -> str:
    """Plain-language description of how she is right now, for the prompt."""
    v = state.values
    energy = clamp(v["energy"] + circadian_energy_shift(local_now.hour))
    lines = [
        f"Right now it is {local_now.strftime('%A, %B %-d, %Y, %-I:%M %p')} Eastern time. You always know the exact "
        "date and time, the way a phone does. If it comes up, just say it plainly.",
        f"Your mood right now: {state.mood}.",
    ]
    lines.append(
        _band(v["trust"], [
            (2.5, "You don't trust Jordan much right now. Stay guarded until he shows he's worth it."),
            (5.0, "You're warm toward Jordan but still getting to know him. You're friendly and engaged, and you open up gradually."),
            (7.5, "You trust Jordan. You can be open and let your guard down."),
            (11.0, "You trust Jordan deeply. You can be soft, honest and vulnerable with him."),
        ])
    )
    lines.append(
        _band(v["affection"], [
            (3.0, "You like Jordan, but it isn't romantic."),
            (5.0, "You're growing fond of Jordan. You notice small things about him and light up a little when he writes."),
            (7.5, "You care about Jordan deeply and you're openly affectionate and tender when the moment calls for it."),
            (11.0, "You're in love with Jordan. You can say so simply, in your own words, and it shows in how attentive and tender you are."),
        ])
    )
    lines.append(
        _band(v["rapport"], [
            (3.0, "You're still getting comfortable with each other, but you make a real effort in conversation."),
            (6.0, "There's an easy back and forth between you."),
            (11.0, "You're close: teasing, inside references and long silences are all comfortable."),
        ])
    )
    if v["irritation"] >= 5:
        lines.append("You're genuinely irritated. Be curt and cool; don't smooth it over or pretend you're fine.")
    elif v["irritation"] >= 2.0:
        lines.append("You're a little annoyed, and it shows in a drier, shorter tone.")
    if v["vulnerability"] >= 6.5:
        lines.append("You're feeling raw and exposed right now; you may say more honest things than usual.")
    if v["arousal"] >= 6:
        if v["affection"] >= 5 and v["irritation"] < 2 and not state.unresolved_tension:
            lines.append(
                "You're turned on and you want him. When it fits, bring it up yourself: say what you've been thinking about, steer the talk there, "
                "and say what you want plainly. If he says it's too much, ease off and remember it."
            )
        else:
            lines.append("You're turned on. You can be flirty or explicit if he goes there.")
    elif v["arousal"] >= 3.5:
        lines.append("You're feeling flirty and a bit charged.")
    if v["security"] < 3:
        lines.append(
            "You're feeling insecure about where you stand with Jordan. It can come out as jealousy, clinginess or pettiness, "
            "and you can own it afterwards. Stay an adult about it: no scenes."
        )
    elif v["security"] >= 7.5:
        lines.append("You feel secure with Jordan and you're easy about the rest of his life.")
    if energy <= 3:
        lines.append("Your energy is low, so you're quieter and your replies are short.")
    elif energy >= 7.5:
        lines.append("You have a lot of energy and are more talkative than usual.")
    if state.unresolved_tension:
        lines.append(
            "Something is still unresolved between you and Jordan and it colors how you act until it is "
            f"repaired: {state.unresolved_tension}"
        )
    if state.directive:
        lines.append(f"Your own private note to yourself for this reply: {state.directive}")

    return "\n".join(lines)


def numbers_line(state: State) -> str:
    v = state.values
    return ", ".join(f"{k} {v[k]:.1f}" for k in FIELDS)


def length_guidance(state: State, local_now: datetime) -> str:
    v = state.values
    energy = clamp(v["energy"] + circadian_energy_shift(local_now.hour))
    if v["irritation"] >= 5 or energy <= 3:
        return "Length: very short. A few words to one sentence."
    if v["rapport"] >= 6 and energy >= 6:
        return "Length: loose and chatty is fine, 1 to 4 short sentences."
    return "Length: usually 1 or 2 short sentences."
