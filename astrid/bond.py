"""How quickly she can warm to him: people differ, and days differ.

Chemistry is rolled once for the relationship (most people are about average, some guarded, a few click fast). Each day gets its
own openness on top of it, carrying over some of yesterday's and dipping when she's insecure or annoyed. Together they set how much
trust, rapport and affection a day can add. When the day's allowance is used up, further warmth still counts, only a lot less,
so a long good chat tops out instead of running to "in love". Falling out is never limited."""
from __future__ import annotations

import json
import random

GAINS = ("trust", "rapport", "affection")
ALLOWANCE = {"trust": 1.2, "rapport": 1.8, "affection": 1.0}  # a typical day, at chemistry 1 and openness 1
LEFTOVER = 0.03  # share of a gain that still lands once the day's allowance is spent
CHEMISTRY = ((0.20, 0.6), (0.70, 1.0), (0.92, 1.4), (1.01, 2.0))  # guarded, typical, open, a spark


def roll_chemistry(rng: random.Random) -> float:
    r = rng.random()
    for limit, value in CHEMISTRY:
        if r < limit:
            return value
    return 1.0


def roll_openness(rng: random.Random, yesterday: float | None, values: dict) -> float:
    fresh = rng.uniform(0.4, 1.5)
    o = fresh if yesterday is None else 0.6 * fresh + 0.4 * yesterday
    if values.get("security", 6) < 4:
        o *= 0.75
    if values.get("irritation", 1) >= 3:
        o *= 0.6
    return round(max(0.2, min(1.6, o)), 2)


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
        room = max(0.0, ALLOWANCE[k] * plan["chem"] * plan["open"] - spent.get(k, 0.0))
        landed = min(d, room) + LEFTOVER * max(0.0, d - room)
        spent[k] = round(spent.get(k, 0.0) + landed, 3)
        out[k] = round(landed, 3)
    return out


async def todays_plan(db, local_date: str, values: dict, rng: random.Random | None = None) -> dict:
    rng = rng or random.Random()
    raw = await db.get_setting("bond_plan")
    plan = json.loads(raw) if raw else {}
    if "chem" not in plan:
        plan["chem"] = roll_chemistry(rng)
    if plan.get("day") != local_date:
        plan.update(day=local_date, open=roll_openness(rng, plan.get("open"), values), spent={})
    return plan


async def save_plan(db, plan: dict) -> None:
    await db.set_setting("bond_plan", json.dumps(plan))
