"""How quickly she can warm to him: people differ, and how things stand differs.

Chemistry is rolled once for the relationship, at the first exchange (most people are about average, some guarded, a few click
fast). On top of it, how open she is right now comes from the state itself (see openness). Together they set how much trust,
rapport and affection a day can add. When the day's allowance is used up, further warmth still counts, only a lot less,
so a long good chat tops out instead of running to "in love". Falling out is never limited."""
from __future__ import annotations

import json
from datetime import date
import random

GAINS = ("trust", "rapport", "affection")
ALLOWANCE = {"trust": 1.2, "rapport": 1.8, "affection": 1.0}  # a typical day, at chemistry 1 and openness 1
LEFTOVER = 0.03  # share of a gain that still lands once the day's allowance is spent
FADE_DAYS = 7  # days for the first impression's pull to halve; after a few weeks only how things actually went matters
CHEMISTRY = ((0.20, 0.6), (0.70, 1.0), (0.92, 1.4), (1.01, 2.0))  # guarded, typical, open, a spark


def roll_chemistry(rng: random.Random) -> float:
    r = rng.random()
    for limit, value in CHEMISTRY:
        if r < limit:
            return value
    return 1.0


def chemistry_now(plan: dict, local_date: str) -> float:
    """The first-meeting roll is only a starting lean: it fades toward neutral as they get to know each other."""
    try:
        days = max(0, (date.fromisoformat(local_date) - date.fromisoformat(plan["since"])).days)
    except (KeyError, ValueError):
        days = 0
    return round(1.0 + (plan["chem"] - 1.0) * 0.5 ** (days / FADE_DAYS), 3)


def openness(values: dict, tension: bool = False) -> float:
    """How easy it is for her to deepen things right now. Not random: it comes from what's built up between them and how she is.
    Trust and rapport already earned make it easier, arousal throws caution to the wind, feeling raw lets her open up, and
    insecurity, irritation or something unresolved closes her off."""
    v = values
    o = 0.6 + 0.08 * (v.get("trust", 4) - 4) + 0.06 * (v.get("rapport", 3) - 3)
    if v.get("arousal", 1) >= 5:
        o += 0.25
    if v.get("vulnerability", 3) >= 6:
        o += 0.15
    if v.get("security", 6) < 4:
        o -= 0.25
    if v.get("irritation", 1) >= 3 or tension:
        o -= 0.35
    return round(max(0.3, min(1.6, o)), 2)


def gate(deltas: dict, plan: dict) -> dict:
    """Trim this appraisal's gains to what's left of today's allowance. `plan` is {'chem', 'open', 'spent'} and is updated in place."""
    out = dict(deltas or {})
    spent = plan.setdefault("spent", {})
    for k in GAINS:
        try:
            d = float(out.get(k, 0) or 0)
        except (TypeError, ValueError):
            continue
        if d <= 0:
            continue
        room = max(0.0, ALLOWANCE[k] * plan.get("chem_now", plan["chem"]) * plan["open"] - spent.get(k, 0.0))
        landed = min(d, room) + LEFTOVER * max(0.0, d - room)
        spent[k] = round(spent.get(k, 0.0) + landed, 3)
        out[k] = round(landed, 3)
    return out


async def todays_plan(db, local_date: str, values: dict, tension: bool = False, rng: random.Random | None = None) -> dict:
    rng = rng or random.Random()
    raw = await db.get_setting("bond_plan")
    plan = json.loads(raw) if raw else {}
    if "chem" not in plan:
        plan["chem"] = roll_chemistry(rng)
        plan["since"] = local_date
    plan.setdefault("since", local_date)               # a plan saved before the fade existed starts counting today
    if plan.get("day") != local_date:
        plan.update(day=local_date, spent={})
    plan["chem_now"] = chemistry_now(plan, local_date)
    plan["open"] = openness(values, tension)           # worked out fresh each time, never stored
    return plan


async def save_plan(db, plan: dict) -> None:
    await db.set_setting("bond_plan", json.dumps({k: v for k, v in plan.items() if k not in ("open", "chem_now")}))
