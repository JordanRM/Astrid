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
import re
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .state import apply_deltas, decayed

log = logging.getLogger("astrid.outreach")

BASE_P = 0.03            # per 5-minute check, before urge, mood and hour are applied
MAX_DAILY = 6
SILENCE_START_H = 3      # she can start to miss him after this many quiet hours
SILENCE_RAMP_H = 18      # and it's at full pull this much later
ACTIVE_MINUTES = 25      # she doesn't text into a conversation that's still going: nothing from either of you in this long


BUSY = re.compile(r"\b(busy|in a meeting|meeting|can'?t (?:talk|chat|text)|can'?t (?:right now|rn)|later|in a bit|in a while|in a few|ttyl|"
                  r"talk soon|gotta (?:go|run)|swamped|slammed|driving|at work|after (?:work|dinner)|when i'?m (?:free|home|done))\b", re.I)
THIN_WORDS = 8       # a reply this short, with nothing after it, is "thin"
THIN_WAITS_H = (5, 14, 36)   # how long she leaves it after one, two, three thin replies in a row
BUSY_WAIT_H = 4      # he said he's busy and would be back: she gives him a few hours first


def classify(mine: list[str]) -> str:
    """The word-list reading of how he answered, used when the model can't be asked."""
    words = sum(len(t.split()) for t in mine)
    if len(mine) <= 3 and words <= 25 and any(BUSY.search(t) for t in mine):
        return "busy"
    if len(mine) <= 2 and words <= THIN_WORDS:
        return "thin"
    return "engaged"


def reaction(values: dict, kind: str, n: int) -> dict:
    """What being met with a thin answer ('thin') or no answer ('none') does to her, as changes to her state. It depends on how she
    already is: the more she cares the more it lands, a shaky or already prickly mood takes it harder, a steady one shrugs it off,
    and it comes out as hurt or as annoyance depending on where she's at. `n` is how many in a row."""
    aff, sec, irr, en = (values.get(k, d) for k, d in (("affection", 1.0), ("security", 6.0), ("irritation", 1.0), ("energy", 6.0)))
    k = {"thin": 0.25, "none": 0.45}[kind] * (0.4 + aff / 8) * (1.5 if sec < 5 else 1.0) * (0.6 if sec >= 7.5 and en >= 6 and irr < 2 else 1.0)
    k *= 1 + 0.3 * (max(1, n) - 1)
    if irr >= 2:                                   # already on edge: it comes out as annoyance
        return {"irritation": round(k * 0.9, 2), "security": round(-k * 0.3, 2)}
    if sec < 6.5:                                  # unsure of him already: it stings
        return {"security": round(-k, 2), "vulnerability": round(k * 0.3, 2), "irritation": round(k * 0.2, 2)}
    return {"security": round(-k * 0.4, 2), "irritation": round(k * 0.3, 2)}   # steady: a small pang


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
        self.speed = getattr(cfg, "outreach_speedup", 1.0) or 1.0   # testing only: silences and waits count this many times faster
        self._stop = False

    def _el(self, seconds: float) -> float:
        return seconds * self.speed

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
        silence_h = self._el(now - last_user) / 3600 if last_user else 0.0
        log_rows = await self._log()
        shared = {r.get("thought") for r in log_rows if r.get("thought")}

        if self.memory is not None:
            await self.memory.load()
            ends = [r for r in self.memory._rows if r["kind"] == "loose_end" and not r["resolved"] and not r.get("followed_up")
                    and r["status"] == "active" and 16 <= self._el(now - r["created_at"]) / 3600 <= 10 * 24]
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

        if last_user and silence_h >= SILENCE_START_H:
            # Starts after a few quiet hours and is full by about a day. The closer she is, the more it pulls, but even a
            # light bond has a little (acquaintances still send each other things).
            urge = clamp((silence_h - SILENCE_START_H) / SILENCE_RAMP_H) * max(0.25, clamp(v["affection"] / 8)) * (1.3 if v["security"] < 4 else 1.0)
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
        return [BASE_P * self.speed * p.urge * (0.3 + self.tide.level) * self._time_factor(p, local, state) for p in plans]

    # ----- deciding -----
    async def _judge(self, hers: str, mine: list[str]) -> str | None:
        """Ask the fast model how he took her text: busy (said he's busy or will be back), brief (a word or two, no real answer), or engaged."""
        msgs = [{"role": "system", "content": "You read a short text exchange. She texted first. Say how he answered, with ONE word: "
                 "busy (he said he's busy, can't talk, or will get back to her later), brief (a word or two, no real engagement, the chat "
                 "went nowhere), or engaged (he actually talked)."},
                {"role": "user", "content": f"Her text: {hers}\nHis replies:\n" + "\n".join(f"- {t}" for t in mine[-6:])}]
        for extra in ({"reasoning": {"enabled": False}}, None):
            try:
                out = (await self.engine.llm.chat(self.cfg.interactor_model, msgs, temperature=0, max_tokens=12, timeout=30, extra=extra)).strip().lower()
            except Exception:
                continue
            for word, kind in (("busy", "busy"), ("brief", "thin"), ("engaged", "engaged")):
                if out.startswith(word):
                    return kind
        return None

    async def _how(self, chat_id, rows: list[dict], i: int, now: float) -> str:
        """How he took the text in rows[i]: none (no answer), busy, thin (a word or two and the chat died), or engaged. Once the chat has
        gone quiet the model's reading is kept with the text, so it's judged once."""
        row = rows[i]
        if row.get("how"):
            return row["how"]
        end = rows[i + 1]["at"] if i + 1 < len(rows) else None
        msgs = await self.db.messages_after(chat_id, 0, end, 60, since_ts=row["at"])
        mine = [m["content"] for m in msgs if m["role"] == "user"]
        if not mine:
            return "none"
        kind = classify(mine)
        if self._el(now - max(float(m["ts"]) for m in msgs)) < 45 * 60:
            return kind                                   # still going: don't settle it yet
        hers = next((m["content"] for m in msgs if m["role"] == "assistant"), "")
        kind = await self._judge(hers, mine) or kind
        row["how"] = kind
        await self.db.set_setting("outreach_log", json.dumps(rows))
        return kind

    async def _react(self, now: float) -> None:
        """Once it's clear how her last text landed, a thin answer or none at all leaves a mark on her mood (once)."""
        rows = await self._log()
        chat_id = await self.db.last_chat_id()
        if not rows or chat_id is None or rows[-1].get("felt"):
            return
        kind = await self._how(chat_id, rows, len(rows) - 1, now)
        if kind == "none" and self._el(now - rows[-1]["at"]) < 6 * 3600:
            return
        if kind in ("thin", "none") and (kind == "none" or rows[-1].get("how")):
            n = 1
            for r in reversed(rows[:-1]):
                if r.get("felt") in ("thin", "none"):
                    n += 1
                else:
                    break
            state = decayed(await self.db.get_state(), now)
            new, applied = apply_deltas(state, reaction(state.values, kind, n))
            await self.db.save_state(new, now, "outreach", applied, f"my text got {'no answer' if kind == 'none' else 'a thin answer'}")
            log.info("her text got %s answer (%d in a row): %s", "no" if kind == "none" else "a thin", n, applied)
        elif kind not in ("engaged", "busy") or not rows[-1].get("how"):
            return
        rows[-1]["felt"] = kind
        await self.db.set_setting("outreach_log", json.dumps(rows))

    async def _cool_off(self, now: float, rows: list[dict]) -> str | None:
        """Why she leaves it for now after a text that got a thin or "busy, later" answer, or None."""
        chat_id = await self.db.last_chat_id()
        if chat_id is None or not rows:
            return None
        kinds = []
        for i in range(len(rows) - 1, max(len(rows) - 4, -1), -1):
            kinds.append(await self._how(chat_id, rows, i, now))
            if kinds[-1] != "thin":
                break
        since = self._el(now - rows[-1]["at"])
        if kinds[0] == "busy":
            return "he said he was busy, giving him a few hours" if since < BUSY_WAIT_H * 3600 * self.rng.uniform(0.8, 1.4) else None
        if kinds[0] == "thin":
            n = sum(1 for k in kinds if k == "thin")
            if since < THIN_WAITS_H[min(n, 3) - 1] * 3600 * self.rng.uniform(0.8, 1.4):
                return f"his answer was short ({n} in a row), leaving room"
        return None

    async def _gate(self, now: float, local: datetime, last_user: float | None) -> str | None:
        """None if she may reach out at all right now, otherwise why not."""
        if not self.cfg.outreach:
            return "off"
        if last_user is None:
            return "he hasn't ever written"
        last_any = await self.db.last_message_at()
        if last_any and self._el(now - last_any) < ACTIVE_MINUTES * 60:
            return f"mid-conversation (last message {int((now - last_any) // 60)} min ago; she waits {ACTIVE_MINUTES})"
        rows = await self._log()
        today = [r for r in rows if datetime.fromtimestamp(r["at"], self.tz).date() == local.date()]
        if len(today) >= self.tide.cap():
            return "enough for today"
        if rows:
            since = self._el(now - rows[-1]["at"])
            unanswered = sum(1 for r in rows if r["at"] > last_user)
            if unanswered >= 3 and since < 72 * 3600:
                return "no answer to the last few, leaving it"
            if unanswered >= 2 and since < 36 * 3600:
                return "no answer to the last two, waiting"
            if since < 90 * 60 * self.rng.uniform(0.7, 1.5):
                return "just texted"
            cool = await self._cool_off(now, rows)
            if cool:
                return cool
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
        rows = await self._log()
        if rows and await self._how(chat_id, rows, len(rows) - 1, now) == "busy":
            note += ("\nLast time you texted, he said he was busy and would get back to you. He hasn't yet. A light, easy nudge is fine, "
                     "with no guilt and nothing that expects an answer.")
        text = await self.engine.reach_out(chat_id, note, plan.heat, plan.cues)
        if not text:
            return None
        await self.send(chat_id, text)
        rows = (await self._log())[-39:] + [{"at": self.now(), "reason": plan.reason, "quiet": quiet, "thought": plan.thought_id}]
        await self.db.set_setting("outreach_log", json.dumps(rows))
        await self.db.set_setting("last_outreach", json.dumps({"at": rows[-1]["at"], "quiet": quiet, "reason": plan.reason}))
        if plan.thought_id:
            await self.db.mark_thoughts_surfaced([plan.thought_id])
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

    FORCED = {
        "miss": Plan("miss", 1.0, "You haven't heard from him for a while and you've noticed. Say that you want to hear from him. Warm and plain, no guilt."),
        "want": Plan("want", 1.0, "You want him right now. Say so as wanting, in plain words if that's what it is, and let him respond. You don't know "
                                  "what he's doing.", heat=True, cues=["wanting him", "what we did"]),
    }

    async def tick(self) -> str | None:
        await self._react(self.now())
        plan = await self.decide()
        return await self.reach_out(plan) if plan else None

    async def loop(self) -> None:
        if self.speed > 1:
            log.warning("outreach TEST SPEED x%g: silences and waits count faster than real time. Remove OUTREACH_SPEEDUP from .env to go back", self.speed)
        await asyncio.sleep(60)
        while not self._stop:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("outreach check skipped: %s", e)
            await asyncio.sleep(300)
