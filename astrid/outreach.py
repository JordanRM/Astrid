"""Her reaching out first. Not a timer: a few things can make her want to text, and whether she does depends on how she feels,
how much the two of them have been talking, and the hour. Some days she texts several times, some days not at all.

Reasons she has (never random trivia):
- follow_up: something he left open (he said he'd tell her how Friday went), once, when it's had time.
- share: something she read on her own that she has a real reason to tell him.
- miss: it's been a while and she's attached.
- want: she's turned on (from how the chat left her, her reading, what happened between them lately).

Quiet hours are a soft line, not a wall: late at night and early morning she mostly doesn't, but something she really wants to say
can still go, and she knows he's asleep and doesn't expect an answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .state import decayed

log = logging.getLogger("astrid.outreach")

BASE_P = 0.03            # per 5-minute check, before urge, mood and hour are applied
MAX_DAILY = 6


def quiet_window(local: datetime) -> bool:
    """Roughly 10pm to 7:30am, later on Friday and Saturday nights and on weekend mornings."""
    wd, t = local.weekday(), local.hour + local.minute / 60
    return t >= (23.0 if wd in (4, 5) else 22.0) or t < (9.0 if wd in (5, 6) else 7.5)


def workday(local: datetime) -> bool:
    return local.weekday() < 5 and 9 <= local.hour < 17


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class Tide:
    """Ebb and flow: how much she feels like reaching out lately. It carries over from day to day, so busy stretches and
    quiet ones come in runs, shaped by how much the two of them have actually been talking."""

    def __init__(self, rng: random.Random, level: float = 0.5, day: str | None = None):
        self.rng, self.level, self.day = rng, level, day
        self._cap = 2

    def for_day(self, local_date, engagement: float) -> float:
        key = local_date.isoformat()
        if self.day != key:
            fresh = clamp(0.1 + 0.75 * engagement + self.rng.uniform(-0.4, 0.4))
            self.level = clamp(0.55 * self.level + 0.45 * fresh)
            self.day = key
            self._cap = 0 if self.level < 0.18 else max(1, min(MAX_DAILY, round(self.level * MAX_DAILY + self.rng.uniform(-0.6, 0.6))))
        return self.level

    def cap(self) -> int:
        """How many unprompted texts she might send today. Settled once a day, so it doesn't wobble."""
        return self._cap

    def dump(self) -> str:
        return json.dumps({"level": self.level, "day": self.day, "cap": self._cap})


@dataclass
class Plan:
    reason: str
    urge: float
    note: str
    heat: bool = False
    cues: list | None = None
    memory_id: int | None = None
    thought_id: int | None = None


def desire(state, explicit_thought_recent: bool, explicit_memory_recent: bool, local: datetime) -> float:
    v = state.values
    d = v["arousal"] + 1.5 * explicit_thought_recent + (1.5 if explicit_memory_recent and v["affection"] >= 4 else 0.0)
    return d + (0.5 if local.hour >= 19 or local.hour < 2 else 0.0)


def ago(hours: float) -> str:
    return f"{int(hours)} hours" if hours < 36 else f"{int(hours // 24)} days"


class Outreach:
    def __init__(self, cfg, db, engine, send, memory=None, clock=time.time, rng: random.Random | None = None):
        self.cfg, self.db, self.engine, self.send, self.memory = cfg, db, engine, send, memory
        self.now = clock
        self.rng = rng or random.Random()
        self.tz = ZoneInfo(cfg.timezone)
        self.tide = Tide(self.rng)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    # ----- bookkeeping -----
    async def _log(self) -> list[dict]:
        try:
            return json.loads(await self.db.get_setting("outreach_log") or "[]")
        except ValueError:
            return []

    async def _load_tide(self) -> None:
        try:
            d = json.loads(await self.db.get_setting("outreach_tide") or "{}")
            self.tide.level, self.tide.day, self.tide._cap = float(d.get("level", 0.5)), d.get("day"), int(d.get("cap", 2))
        except ValueError:
            pass

    # ----- what she might want to say -----
    async def urges(self, now: float, state, local: datetime, last_user: float | None) -> list[Plan]:
        v = state.values
        plans: list[Plan] = []
        silence_h = (now - last_user) / 3600 if last_user else 0.0
        log_rows = await self._log()
        shared = {r.get("thought") for r in log_rows if r.get("thought")}

        if self.memory is not None:
            await self.memory.load()
            ends = [r for r in self.memory._rows if r["kind"] == "loose_end" and not r["resolved"] and not r.get("followed_up")
                    and r["status"] == "active" and 16 <= (now - r["created_at"]) / 3600 <= 10 * 24]
            if ends:
                best = max(ends, key=lambda r: (now - r["created_at"]))
                age_days = (now - best["created_at"]) / 86400
                plans.append(Plan("follow_up", clamp(0.25 + age_days * 0.2) * (1.0 if best["importance"] >= 4 else 0.65),
                                  f"Something he left open, which you've remembered: {best['text']} You want to know how it went. Ask once, in your own "
                                  "words, the way someone who remembered would, not as a reminder.", cues=[best["text"]], memory_id=best["id"]))

        thoughts = await self.db.list_thoughts(30)
        sharable = [t for t in thoughts if t.get("for_jordan") and not t.get("explicit") and not t.get("surfaced") and t["id"] not in shared
                    and now - float(t["created_at"]) < 3 * 86400]
        if sharable:
            t = max(sharable, key=lambda t: float(t["share"]))
            plans.append(Plan("share", clamp(float(t["share"]) * (0.35 + 0.065 * v["rapport"])),
                              f"On your own time you read about this: {t['question']} What you made of it: {t['thought']} Why you'd tell him: {t['for_jordan']} "
                              "Tell him the one part that matters, in your words. Not a summary, not a link.", cues=[t["question"], t["for_jordan"]],
                              thought_id=t["id"]))

        if last_user and silence_h >= 12:
            urge = clamp((silence_h - 12) / 60) * clamp(v["affection"] / 8) * (1.3 if v["security"] < 4 else 1.0)
            tone = ("You're a little hurt that it's been quiet, but you want him to talk to you, not feel guilty. Say it dry and plainly."
                    if v["security"] < 4 else "Say that you noticed the quiet and you want to hear from him. Warm and plain, no guilt.")
            plans.append(Plan("miss", clamp(urge), f"You haven't heard from him in {ago(silence_h)} and you've noticed. {tone}"))

        explicit_thoughts = [t for t in thoughts if t.get("explicit") and now - float(t["created_at"]) < 86400]
        explicit_mem = self.memory is not None and any(r["explicit"] and now - r["created_at"] < 3 * 86400 for r in self.memory._rows)
        d = desire(state, bool(explicit_thoughts), explicit_mem, local)
        if v["irritation"] < 4 and d >= 4.5:
            busy = ("It's a weekday in working hours and he may well be in the middle of something. You don't know. That can be part of why "
                    "you're texting, a little bold about it." if workday(local) else "You don't know what he's doing right now.")
            plans.append(Plan("want", clamp((d - 4) / 4), f"You want him right now. Say so as wanting, in plain words if that's what it is, and let him "
                              f"respond. {busy}", heat=True, cues=["wanting him", "what we did"]))
        return [p for p in plans if p.urge > 0.1]

    def _time_factor(self, plan: Plan, local: datetime, state) -> float:
        if quiet_window(local):
            if plan.reason == "want":
                return 0.15 if state.values["arousal"] >= 6 else 0.03
            return 0.06 if plan.reason == "miss" else 0.02
        return 0.5 if workday(local) and plan.reason != "want" else 1.0

    def chances(self, plans: list[Plan], local: datetime, state) -> list[float]:
        """The chance, at each 5-minute check, that she acts on each thing she has in mind."""
        return [BASE_P * p.urge * (0.3 + self.tide.level) * self._time_factor(p, local, state) for p in plans]

    # ----- deciding -----
    async def _gate(self, now: float, local: datetime, last_user: float | None) -> str | None:
        """None if she may reach out at all right now, otherwise why not."""
        if not self.cfg.outreach:
            return "off"
        if last_user is None:
            return "he hasn't ever written"
        last_any = await self.db.last_message_at()
        if last_any and now - last_any < 25 * 60:
            return "mid-conversation"
        rows = await self._log()
        today = [r for r in rows if datetime.fromtimestamp(r["at"], self.tz).date() == local.date()]
        if len(today) >= self.tide.cap():
            return "enough for today"
        if rows:
            since = now - rows[-1]["at"]
            unanswered = sum(1 for r in rows if r["at"] > last_user)
            if unanswered >= 3 and since < 72 * 3600:
                return "no answer to the last few, leaving it"
            if unanswered >= 2 and since < 36 * 3600:
                return "no answer to the last two, waiting"
            if since < 90 * 60 * self.rng.uniform(0.7, 1.5):
                return "just texted"
        return None

    async def decide(self, now: float | None = None) -> Plan | None:
        now = now or self.now()
        local = datetime.fromtimestamp(now, self.tz)
        last_user = await self.db.last_user_message_at()
        await self._load_tide()
        engagement = clamp(0.6 * (await self.db.user_message_count(now - 3 * 86400)) / 40 +
                           0.4 * (lambda s: (s.values["rapport"] + s.values["affection"]) / 20)(decayed(await self.db.get_state(), now)))
        self.tide.for_day(local.date(), engagement)
        await self.db.set_setting("outreach_tide", self.tide.dump())
        why = await self._gate(now, local, last_user)
        if why:
            return None
        state = decayed(await self.db.get_state(), now)
        plans = await self.urges(now, state, local, last_user)
        if not plans:
            return None
        ps = self.chances(plans, local, state)
        miss_all = 1.0
        for p in ps:
            miss_all *= 1 - p
        if self.rng.random() >= 1 - miss_all:
            return None
        return self.rng.choices(plans, weights=ps)[0]

    # ----- doing it -----
    async def reach_out(self, plan: Plan, now: float | None = None) -> str | None:
        now = now or self.now()
        local = datetime.fromtimestamp(now, self.tz)
        chat_id = await self.db.last_chat_id()
        if chat_id is None:
            return None
        quiet = quiet_window(local)
        note = plan.note + (
            f"\nIt's {local.strftime('%A %-I:%M %p')} for him, the middle of the night or very early. He's probably asleep. You don't expect "
            "an answer, so don't ask 'are you up' and don't make it about him replying. Say it and let it sit." if quiet
            else f"\nIt's {local.strftime('%A %-I:%M %p')}.")
        text = await self.engine.reach_out(chat_id, note, plan.heat, plan.cues)
        if not text:
            return None
        await self.send(chat_id, text)
        rows = (await self._log())[-39:] + [{"at": self.now(), "reason": plan.reason, "quiet": quiet, "thought": plan.thought_id}]
        await self.db.set_setting("outreach_log", json.dumps(rows))
        await self.db.set_setting("last_outreach", json.dumps({"at": rows[-1]["at"], "quiet": quiet, "reason": plan.reason}))
        if plan.memory_id and self.memory is not None:
            for r in self.memory._rows:
                if r["id"] == plan.memory_id:
                    r["followed_up"] = True
            await self.db.update_memory(plan.memory_id, {"followed_up": True})
        if plan.reason == "want":   # she's in the mood now, so if he answers she's already there
            st = decayed(await self.db.get_state(), now)
            st.values["arousal"] = max(st.values["arousal"], min(7.0, 4.0 + 3 * plan.urge))
            await self.db.save_state(st, now, "outreach", {"arousal": st.values["arousal"]}, "texted him first, wanting him")
        log.info("reached out (%s%s): %r", plan.reason, ", quiet hours" if quiet else "", text[:80])
        return text

    async def tick(self) -> str | None:
        plan = await self.decide()
        return await self.reach_out(plan) if plan else None

    async def loop(self) -> None:
        await asyncio.sleep(60)
        while not self._stop:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("outreach check skipped: %s", e)
            await asyncio.sleep(300)
