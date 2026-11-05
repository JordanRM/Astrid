"""Her long-term memory: what happened, how it felt, how strongly it sticks, and what it leads to.

How it behaves, in human terms:
- After each exchange something small may be kept (a fact, a moment, something left open). Most exchanges keep nothing.
- A short, rewritable profile of Jordan is always in her head. Everything else comes back by cue: similar meaning,
  how strong the moment was, how recent, and a little by mood. A memory that surfaces can bring one linked memory with it (one hop, never a chain).
- Remembering strengthens a memory. Left alone, memories fade: details go first, then only a hazy sense is left.
  Strong moments (importance 8+) never fade.
- Quiet stretches get summed up as an episode. Overnight, small related memories merge into a gist.
- Sexual moments are stored like any other memory, in plain words, so she can bring them back in the right moment.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .appraisal import _extract_json
from .embed import Embedder
from .llm import LLMError

log = logging.getLogger("astrid.memory")

HALF_LIFE_DAYS = {"fact": 90, "moment": 45, "loose_end": 14, "episode": 21, "gist": 120}
PERMANENT_AT = 8          # importance at or above this never fades
HAZY_BELOW = 0.35         # below this strength only a vague sense is left
FORGOTTEN_BELOW = 0.05    # below this it is let go (kept in the database, no longer recalled)
RECALL_GAIN = 0.15
MAX_PROFILE_CHARS = 1400
KINDS = ("fact", "moment", "loose_end")


# ---------- pure helpers ----------
def clean(text, n: int = 400) -> str:
    s = " ".join(str(text or "").split())
    s = re.sub(r"\s*[—–]\s*", ", ", s)
    return s[:n]


def clamp(x, lo, hi, default=0.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return default


def effective_strength(m: dict, now: float) -> float:
    """How strongly a memory holds right now. Strong moments don't fade; the rest halve every so often unless recalled."""
    if m.get("permanent"):
        return max(1.0, float(m["strength"]))
    anchor = m.get("last_recalled_at") or m["created_at"]
    days = max(0.0, (now - anchor) / 86400)
    half = HALF_LIFE_DAYS.get(m["kind"], 60) * (0.5 + float(m["importance"]) / 6)
    return float(m["strength"]) * 0.5 ** (days / half)


def is_hazy(m: dict, now: float) -> bool:
    return not m.get("permanent") and effective_strength(m, now) < HAZY_BELOW


def when_text(created_at: float, now: float, tz: ZoneInfo) -> str:
    d = (datetime.fromtimestamp(now, tz).date() - datetime.fromtimestamp(created_at, tz).date()).days
    if d <= 0:
        return "earlier today"
    if d == 1:
        return "yesterday"
    if d < 7:
        return f"{d} days ago"
    if d < 14:
        return "about a week ago"
    if d < 60:
        return f"about {d // 7} weeks ago"
    return f"about {max(2, d // 30)} months ago"


class MoodLean:
    """Which way her memory leans right now: toward old hurts (-1), toward good times (+1), or neither (0).

    Not recomputed every turn. A lean is rolled from how she actually is (irritation, security, anything unresolved between
    them, warmth, closeness) and then lingers for a while, the way a bad mood does. Most of the time there is none.
    A bad lean only lets go once things are properly repaired; a good one fades when warmth drops or something sours it.
    """

    def __init__(self, rng):
        self.rng = rng
        self.lean = 0
        self.strength = 0.0
        self.until = 0.0
        self._irritation = 0.0
        self._tension = False

    def _roll(self, state, now: float) -> None:
        v = state.values
        tension = bool(state.unresolved_tension)
        p_bad = clamp((v["irritation"] - 3) / 6 + (5 - v["security"]) / 14 + (0.15 if tension else 0.0), 0, 0.7)
        p_good = clamp((v["rapport"] + v["affection"] - 10) / 14, 0, 0.45) * (1.0 if v["irritation"] < 3 else 0.3)
        r = self.rng.random()
        if r < p_bad:
            self.lean, self.strength = -1, 0.5 + 0.5 * self.rng.random()
            self.until = now + self.rng.uniform(60, 240) * 60
        elif r < p_bad + p_good:
            self.lean, self.strength = 1, 0.5 + 0.5 * self.rng.random()
            self.until = now + self.rng.uniform(45, 150) * 60
        else:
            self.lean, self.strength = 0, 0.0
            self.until = now + self.rng.uniform(40, 120) * 60
        self._irritation, self._tension = v["irritation"], tension

    def current(self, state, now: float) -> tuple[float, float]:
        """(pull toward bad memories, pull toward good ones), each 0..1."""
        v = state.values
        tension = bool(state.unresolved_tension)
        shifted = (v["irritation"] - self._irritation >= 2.5) or (tension and not self._tension)
        if now >= self.until or shifted:
            self._roll(state, now)
        elif self.lean < 0 and v["irritation"] < 2.5 and not tension:
            self.lean, self.strength, self.until = 0, 0.0, now + self.rng.uniform(40, 120) * 60   # repaired: it lets go
        elif self.lean > 0 and (v["irritation"] > 5 or v["rapport"] + v["affection"] < 8):
            self.lean, self.strength, self.until = 0, 0.0, now + self.rng.uniform(40, 120) * 60   # the warmth went out of it
        return (self.strength if self.lean < 0 else 0.0, self.strength if self.lean > 0 else 0.0)


@dataclass
class Recalled:
    row: dict
    score: float
    via: str = "cue"   # cue | link | drift


# ---------- the memory itself ----------
class Memory:
    def __init__(self, cfg, db, llm, embedder: Embedder, clock=time.time, rng=None):
        import random
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.embedder = embedder
        self.now = clock
        self.rng = rng or random.Random()
        self.mood = MoodLean(self.rng)
        self.tz = ZoneInfo(cfg.timezone)
        self._rows: list[dict] = []
        self._mat: np.ndarray | None = None
        self._profile: list[dict] = []
        self._summary: dict | None = None
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    # ----- loading -----
    async def warm(self) -> None:
        """Load the embedding model now (the first run downloads it), falling back to word matching if it can't load."""
        from .embed import HashEmbedder
        try:
            await self.embedder.embed(["warm up"])
        except Exception as e:
            if isinstance(self.embedder, HashEmbedder):
                raise
            log.warning("embedding model unavailable (%s: %s); using simple word matching for now", type(e).__name__, str(e)[:120])
            self.embedder = HashEmbedder()
        await self.load()
        log.info("memory ready: %d memories, %d profile facts, embedder %s", len(self._rows), len(self._profile), self.embedder.name)

    async def load(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            rows = await self.db.load_memories()
            stale = [r for r in rows if r.get("embedding") is None or r.get("embed_model") != self.embedder.name]
            for i in range(0, len(stale), 32):
                chunk = stale[i:i + 32]
                vecs = await self.embedder.embed([r["text"] for r in chunk])
                for r, v in zip(chunk, vecs):
                    r["embedding"], r["embed_model"] = v, self.embedder.name
                    await self.db.update_memory(r["id"], {"embedding": v, "embed_model": self.embedder.name})
            self._rows = rows
            self._rebuild()
            self._profile = await self.db.profile_list()
            if not self._profile and not await self.db.fetchrow("select 1 from astrid.profile_facts limit 1"):
                await self.db.profile_put("home", "lives in Lanark Village, a small rural village in Ontario, so he's on Eastern time",
                                          6, self.now())
                self._profile = await self.db.profile_list()
            raw = await self.db.get_setting("rolling_summary")
            try:
                self._summary = json.loads(raw) if raw else None
            except ValueError:
                self._summary = None
            self._loaded = True

    def _rebuild(self) -> None:
        self._mat = np.asarray([r["embedding"] for r in self._rows], dtype=np.float32) if self._rows else None

    # ----- what goes into her prompt -----
    async def profile_facts(self) -> list[dict]:
        await self.load()
        return self._profile

    async def summary_text(self, local_date) -> str | None:
        """A running summary of earlier today, for the part of the day that has scrolled out of the recent window."""
        await self.load()
        s = self._summary
        if s and s.get("day") == local_date.isoformat() and s.get("text"):
            return ("Earlier today, before the part of the chat above (a summary, not exact words; you also have what's in the chat "
                    "above):\n" + s["text"])
        return None

    async def recall(self, texts: list[str], state, heat: bool, now: float, limit: int = 5) -> list[Recalled]:
        """Memories that come back given what's being said, how she feels, and what they're linked to."""
        await self.load()
        live = [i for i, r in enumerate(self._rows) if r["status"] == "active"]
        query = clean(" ".join(t for t in texts if t), 700)
        if not live or not query or self._mat is None:
            return []
        qv = np.asarray((await self.embedder.embed([query]))[0], dtype=np.float32)
        sims = self._mat @ qv
        gate = getattr(self.cfg, "memory_min_sim", 0) or self.embedder.min_sim
        neg, pos = self.mood.current(state, now)
        aroused = state.values["arousal"] >= 4
        scored: list[tuple[float, int]] = []
        for i in live:
            m = self._rows[i]
            sim = float(sims[i])
            if sim < gate:
                continue
            eff = effective_strength(m, now)
            if eff < FORGOTTEN_BELOW and not m["permanent"]:
                continue
            rel = (sim - gate) / max(1e-6, 1 - gate)
            age = max(0.0, (now - m["created_at"]) / 86400)
            score = rel + 0.03 * float(m["importance"]) + 0.08 * 0.5 ** (age / 3)
            score += 0.06 * (neg * max(0.0, -float(m["valence"])) + pos * max(0.0, float(m["valence"])))
            score += 0.04 * math.log1p(m["recalled_count"])
            if m["kind"] == "loose_end" and not m["resolved"]:
                score += 0.12
            if m["explicit"]:
                score += 0.05 if heat else (0.0 if aroused or rel > 0.5 else -0.15)
            score *= 0.3 + 0.7 * min(1.0, eff)
            scored.append((score, i))
        scored.sort(reverse=True)
        picked: list[Recalled] = [Recalled(self._rows[i], s) for s, i in scored[:max(1, limit - 1)]]
        # One hop only: a memory that surfaced from the cue may bring along something it is linked to. What that one is
        # linked to does not follow, and mood never starts a chain. Only from the strongest cued ones, and only sometimes.
        chosen = {r.row["id"] for r in picked}
        by_id = {r["id"]: r for r in self._rows if r["status"] == "active"}
        linked: dict[int, float] = {}
        for r in picked[:2]:
            for lid in r.row.get("links") or []:
                other = by_id.get(lid)
                if other and lid not in chosen and (not other["explicit"] or heat or aroused) and self.rng.random() < 0.6:
                    linked[lid] = max(linked.get(lid, 0.0), r.score * 0.6 * (0.3 + 0.7 * min(1.0, effective_strength(other, now))))
        for lid, s_ in sorted(linked.items(), key=lambda kv: -kv[1])[:1]:
            picked.append(Recalled(by_id[lid], s_, "link"))
            chosen.add(lid)
        # now and then something just comes to mind, tied to how she feels
        if len(picked) < 2 and self.rng.random() < 0.12:
            pool = [r for r in by_id.values() if r["id"] not in chosen and (r["permanent"] or r["importance"] >= 6)
                    and (not r["explicit"] or heat or aroused)]
            if pool:
                weights = [0.2 + float(r["importance"]) + 4 * (neg * max(0.0, -float(r["valence"])) + pos * max(0.0, float(r["valence"])))
                           for r in pool]
                picked.append(Recalled(self.rng.choices(pool, weights)[0], 0.0, "drift"))
        picked = picked[:limit + 1]
        await self._strengthen([p.row for p in picked], now)
        return picked

    async def _strengthen(self, rows: list[dict], now: float) -> None:
        """Remembering something makes it stick (at most once an hour per memory, so one topic doesn't pin it forever)."""
        for m in rows:
            if m["last_recalled_at"] and now - m["last_recalled_at"] < 3600:
                continue
            eff = effective_strength(m, now)
            m["strength"] = min(2.0, eff + RECALL_GAIN)
            m["last_recalled_at"] = now
            m["recalled_count"] += 1
            try:
                await self.db.update_memory(m["id"], {"strength": m["strength"], "last_recalled_at": now,
                                                      "recalled_count": m["recalled_count"]})
            except Exception as e:  # remembering must never break a reply
                log.warning("could not save recall of memory %s: %s", m["id"], e)

    def render(self, recalled: list[Recalled], now: float) -> str | None:
        if not recalled:
            return None
        lines = []
        for r in recalled:
            m = r.row
            when = when_text(m["created_at"], now, self.tz)
            if is_hazy(m, now):
                lines.append(f"- ({when}, hazy) {m.get('gist') or 'something he told you once, you can no longer say what'}")
            else:
                feel = f", felt {m['emotion']}" if m.get("emotion") and m["importance"] >= 5 else ""
                lines.append(f"- ({when}{feel}) {m['text']}")
        return ("Things that came to mind from earlier. They surfaced on their own; bring one up only if it genuinely fits the moment, "
                "in your own words, and never recite them as a list or announce that you remember. Older ones may be out of date: people change, and what he says now beats what you remember. "
                "A hazy one is only a vague sense: don't invent details you don't have.\n" + "\n".join(lines))

    # ----- saving -----
    async def _store(self, kind: str, text: str, gist: str | None, importance: float, emotion: str | None, valence: float,
                     explicit: bool, now: float, created_at: float | None = None) -> int | None:
        """Save one memory unless it's already known (then the old one is reinforced). Links it to similar ones."""
        await self.load()
        async with self._write_lock:
            vec = (await self.embedder.embed([text]))[0]
            v = np.asarray(vec, dtype=np.float32)
            sims = (self._mat @ v) if self._mat is not None else np.zeros(0)
            for i in range(len(self._rows)):
                if self._rows[i]["status"] == "active" and sims[i] >= self.embedder.dup_sim and self._rows[i]["kind"] == kind:
                    old = self._rows[i]
                    bump = {"importance": max(float(old["importance"]), importance)}
                    old.update(bump)
                    old["permanent"] = old["permanent"] or old["importance"] >= PERMANENT_AT
                    await self.db.update_memory(old["id"], {**bump, "permanent": old["permanent"]})
                    await self._strengthen([old], now)
                    return None
            near = [i for i in np.argsort(-sims)[:3] if self._rows[i]["status"] == "active" and sims[i] >= self.embedder.link_sim] if len(sims) else []
            mid = await self.db.add_memory({
                "kind": kind, "text": text, "gist": gist, "importance": importance, "emotion": emotion, "valence": valence,
                "explicit": explicit, "permanent": importance >= PERMANENT_AT, "embedding": vec, "embed_model": self.embedder.name,
                "created_at": created_at, "links": [self._rows[i]["id"] for i in near],
            }, now)
            row = {"id": mid, "created_at": created_at or now, "kind": kind, "text": text, "gist": gist, "importance": importance,
                   "emotion": emotion, "valence": valence, "explicit": explicit, "strength": 1.0,
                   "permanent": importance >= PERMANENT_AT, "recalled_count": 0, "last_recalled_at": None, "embedding": vec,
                   "embed_model": self.embedder.name, "links": [self._rows[i]["id"] for i in near], "status": "active", "resolved": False}
            for i in near:
                other = self._rows[i]
                other["links"] = list(other["links"] or []) + [mid]
                await self.db.update_memory(other["id"], {"links": other["links"]})
            self._rows.append(row)
            self._rebuild()
            return mid

    async def _ask(self, system: str, user: str, max_tokens: int = 3000, fast: bool = False) -> dict | None:
        """One JSON answer. `fast` is for plain writing (summaries), which doesn't need the slow reasoning model. A model that
        runs out of tokens while thinking gets one more try with double the room, then the other model gets a go."""
        first, second = (self.cfg.interactor_model, self.cfg.thinking_model) if fast else (self.cfg.thinking_model, self.cfg.interactor_model)
        last: Exception | None = None
        models = list(dict.fromkeys((first, second)))
        for model in models:
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            # Plain writing doesn't need reasoning; turning it off stops the model burning its whole budget thinking.
            extra = {"reasoning": {"enabled": False}} if fast else None
            for attempt in (1, 2):
                try:
                    try:
                        raw = await self.llm.chat(model, msgs, max_tokens=max_tokens * attempt, temperature=0.4, timeout=120, extra=extra)
                    except LLMError as e:
                        if extra is None or str(e).startswith("empty") or not str(e).startswith("HTTP 4"):
                            raise
                        extra = None  # this model won't take the switch; ask the plain way
                        raw = await self.llm.chat(model, msgs, max_tokens=max_tokens * attempt, temperature=0.4, timeout=120)
                except LLMError as e:
                    if not str(e).startswith("empty"):
                        raise
                    last = e
                    continue
                data = _extract_json(raw)
                if data is not None:
                    return data
                msgs = msgs[:2] + [{"role": "user", "content": "Reply with ONLY the JSON object, complete."}]
                break
            else:
                if len(models) > 1:
                    log.info("memory: %s gave nothing usable, trying the other model", model)
        if last is not None:
            raise last
        return None

    async def _related(self, text: str, k: int = 5) -> list[dict]:
        """Existing memories that this exchange might confirm, refine or contradict."""
        if self._mat is None or not self._rows:
            return []
        qv = np.asarray((await self.embedder.embed([clean(text, 700)]))[0], dtype=np.float32)
        sims = self._mat @ qv
        gate = (getattr(self.cfg, "memory_min_sim", 0) or self.embedder.min_sim) * 0.95
        return [self._rows[i] for i in np.argsort(-sims)[:k] if sims[i] >= gate and self._rows[i]["status"] == "active"]

    async def _supersede(self, memory_id: int) -> bool:
        """He changed his mind (or it was wrong): the old memory stays in the database but stops coming back. The new memory says what changed."""
        for r in self._rows:
            if r["id"] == memory_id and r["status"] == "active":
                r["status"] = "superseded"
                await self.db.update_memory(memory_id, {"status": "superseded"})
                self._rows = [x for x in self._rows if x["status"] == "active"]
                self._rebuild()
                return True
        return False

    # ----- learning from an exchange -----
    async def extract(self, exchanges: list[tuple[str, str]], earlier: list[tuple[str, str]], now: float) -> dict:
        """Decide what from these exchanges is worth keeping. Never raises."""
        out = {"saved": 0, "profile": 0}
        try:
            await self.load()
            open_ends = [r for r in self._rows if r["kind"] == "loose_end" and not r["resolved"] and r["status"] == "active"][-8:]
            related = await self._related(" ".join(f"{u} {a}" for u, a in exchanges))
            allowed = {str(r["id"]) for r in related}
            convo = "\n".join(f"{'Jordan' if r == 'user' else 'Astrid'}: {c}" for r, c in earlier[-6:])
            new = "\n".join(f"Jordan: {u}\nAstrid: {a}" for u, a in exchanges)
            body = (
                "What she currently knows about Jordan:\n" + ("\n".join(f"- [{f['key']}] {f['text']}" for f in self._profile) or "(nothing yet)")
                + ("\n\nWhat she already remembers that may relate (use \"replaces\" if this exchange shows one has changed or was wrong):\n"
                   + "\n".join(f"- #{r['id']} ({when_text(r['created_at'], now, self.tz)}): {r['text']}" for r in related) if related else "")
                + ("\n\nThings left open:\n" + "\n".join(f"- #{r['id']}: {r['text']}" for r in open_ends) if open_ends else "")
                + (f"\n\nEarlier in the chat:\n{convo}" if convo else "")
                + f"\n\nThe new exchange{'s' if len(exchanges) > 1 else ''}:\n{new}"
            )
            data = await self._ask(EXTRACT, body)
            if not data:
                return out
            for item in (data.get("memories") or [])[:4]:
                if not isinstance(item, dict):
                    continue
                kind = item.get("kind") if item.get("kind") in KINDS else "fact"
                text = clean(item.get("text"), 420)
                importance = clamp(item.get("importance"), 0, 10, 0)
                if not text or importance < 2:
                    continue
                mid = await self._store(kind, text, clean(item.get("gist"), 140) or None, importance, clean(item.get("emotion"), 40) or None,
                                        clamp(item.get("valence"), -1, 1), bool(item.get("explicit")), now)
                if mid:
                    out["saved"] += 1
                    for rid in (item.get("replaces") or [])[:3]:
                        rid = str(rid).lstrip("#")
                        if rid in allowed and await self._supersede(int(rid)):
                            out["replaced"] = out.get("replaced", 0) + 1
            for item in (data.get("forget") or [])[:2]:
                if isinstance(item, dict) and clean(item.get("about")):
                    about = clean(item.get("about"), 120)
                    await self._store("moment", f"He asked me to drop {about}. I agreed. He wouldn't like it brought up.",
                                      "something he asked me to leave alone", 6, "careful", -0.2, False, now)
                    out["saved"] += 1
            for op in (data.get("profile") or [])[:6]:
                if not isinstance(op, dict):
                    continue
                key = re.sub(r"[^a-z0-9_]+", "_", str(op.get("key") or "").lower()).strip("_")[:40]
                if not key:
                    continue
                if op.get("op") == "remove":
                    out["profile"] += int(await self.db.profile_remove(key, now))
                elif clean(op.get("text"), 220):
                    r = await self.db.profile_put(key, clean(op.get("text"), 220), clamp(op.get("importance"), 1, 10, 5), now)
                    out["profile"] += int(r != "same")
            for rid in (data.get("resolved") or [])[:4]:
                row = next((r for r in self._rows if str(r["id"]) == str(rid).lstrip("#")), None)
                if row and row["kind"] == "loose_end":
                    row["resolved"] = True
                    await self.db.update_memory(row["id"], {"resolved": True})
            if out["profile"]:
                self._profile = await self.db.profile_list()
            if out["saved"] or out["profile"]:
                log.info("memory: kept %d, profile changes %d", out["saved"], out["profile"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("memory extraction skipped: %s", e)
        return out

    # ----- summaries -----
    async def close_episode(self, chat_id: int, now: float, quiet_for: float = 3600) -> bool:
        """After a quiet stretch, keep one memory of how that conversation went."""
        last = await self.db.last_message_at()
        if last is None or now - last < quiet_for:
            return False
        through = int(await self.db.get_setting("episode_through") or 0)
        msgs = await self.db.messages_after(chat_id, through, None, 300)
        if not msgs:
            return False
        await self.db.set_setting("episode_through", str(msgs[-1]["id"]))
        if len(msgs) < 6:
            return False
        convo = "\n".join(f"{'Jordan' if m['role'] == 'user' else 'Astrid'}: {m['content']}" for m in msgs)[-9000:]
        start = datetime.fromtimestamp(msgs[0]["ts"], self.tz)
        data = await self._ask(EPISODE, f"This conversation started {start.strftime('%A %-I:%M %p')}.\n\n{convo}", fast=True)
        text = clean((data or {}).get("summary"), 600)
        if not text:
            return False
        await self._store("episode", text, clean((data or {}).get("gist"), 140) or None, clamp(data.get("importance"), 1, 10, 3),
                          clean(data.get("emotion"), 40) or None, clamp(data.get("valence"), -1, 1), bool(data.get("explicit")),
                          now, created_at=msgs[-1]["ts"])
        log.info("memory: kept an episode (%d messages)", len(msgs))
        return True

    async def roll_summary(self, chat_id: int, now: float) -> bool:
        """Keep a running summary of today's earlier conversation once it has scrolled out of the recent window."""
        await self.load()
        local = datetime.fromtimestamp(now, self.tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        edge = now - self.cfg.history_minutes * 60
        if edge <= midnight:
            return False
        s = self._summary if self._summary and self._summary.get("day") == local.date().isoformat() else None
        through = int(s["through"]) if s else 0
        msgs = await self.db.messages_after(chat_id, through, edge, 60, since_ts=midnight)
        if len(msgs) < 8:
            return False
        convo = "\n".join(f"{'Jordan' if m['role'] == 'user' else 'Astrid'}: {m['content']}" for m in msgs)[-6000:]
        data = await self._ask(ROLLING, (f"Summary so far:\n{s['text']}\n\n" if s else "") + f"New messages to fold in:\n{convo}", fast=True)
        text = clean((data or {}).get("summary"), 900)
        if not text:
            return False
        self._summary = {"day": local.date().isoformat(), "through": msgs[-1]["id"], "text": text}
        await self.db.set_setting("rolling_summary", json.dumps(self._summary))
        return True

    # ----- overnight -----
    async def consolidate(self, now: float) -> dict:
        """Let weak things fade, merge small related memories into gists, tidy the profile."""
        await self.load()
        report = {"faded": 0, "merged": 0, "profile": False}
        for m in self._rows:
            if m["status"] == "active" and not m["permanent"] and effective_strength(m, now) < FORGOTTEN_BELOW:
                m["status"] = "faded"
                await self.db.update_memory(m["id"], {"status": "faded"})
                report["faded"] += 1
        # related, older, ordinary memories become one gist
        pool = [i for i, m in enumerate(self._rows) if m["status"] == "active" and not m["permanent"] and m["importance"] < 7
                and m["kind"] in ("fact", "moment", "episode", "gist") and now - m["created_at"] > 3 * 86400]
        used: set[int] = set()
        clusters = []
        if pool and self._mat is not None:
            sub = self._mat[pool]
            sims = sub @ sub.T
            for a in range(len(pool)):
                if pool[a] in used:
                    continue
                group = [b for b in range(len(pool)) if pool[b] not in used and sims[a, b] >= self.embedder.merge_sim]
                if len(group) >= 3:
                    group = group[:8]
                    used.update(pool[b] for b in group)
                    clusters.append([self._rows[pool[b]] for b in group])
                if len(clusters) >= 4:
                    break
        for members in clusters:
            try:
                listing = "\n".join(f"- ({when_text(m['created_at'], now, self.tz)}) {m['text']}" for m in members)
                data = await self._ask(MERGE, listing, fast=True)
                text = clean((data or {}).get("text"), 500)
                if not text:
                    continue
                ids = {m["id"] for m in members}
                outside = sorted({l for m in members for l in (m["links"] or []) if l not in ids})
                mid = await self._store("gist", text, clean(data.get("gist"), 140) or None,
                                        max(clamp(data.get("importance"), 1, 10, 3), max(float(m["importance"]) for m in members) - 1),
                                        clean(data.get("emotion"), 40) or None, clamp(data.get("valence"), -1, 1),
                                        any(m["explicit"] for m in members), now, created_at=max(m["created_at"] for m in members))
                if mid is None:
                    continue
                for m in members:
                    m["status"] = "merged"
                    await self.db.update_memory(m["id"], {"status": "merged"})
                for lid in outside:
                    await self.db.add_link(mid, lid)
                    for r in self._rows:
                        if r["id"] in (mid, lid):
                            other = lid if r["id"] == mid else mid
                            if other not in (r["links"] or []):
                                r["links"] = list(r["links"] or []) + [other]
                report["merged"] += len(members)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("merge skipped: %s", e)
        self._rows = [r for r in self._rows if r["status"] == "active"]
        self._rebuild()
        report["profile"] = await self.tidy_profile(now)
        return report

    async def tidy_profile(self, now: float) -> bool:
        await self.load()
        total = sum(len(f["text"]) for f in self._profile)
        if total <= MAX_PROFILE_CHARS and len(self._profile) <= 24:
            return False
        listing = "\n".join(f"[{f['key']}] {f['text']}" for f in self._profile)
        data = await self._ask(TIDY, listing)
        facts = (data or {}).get("facts")
        if not isinstance(facts, list) or not facts:
            return False
        keep = {}
        for f in facts[:24]:
            if isinstance(f, dict) and clean(f.get("text"), 220):
                key = re.sub(r"[^a-z0-9_]+", "_", str(f.get("key") or "").lower()).strip("_")[:40]
                if key:
                    keep[key] = (clean(f["text"], 220), clamp(f.get("importance"), 1, 10, 5))
        if not keep or sum(len(t) for t, _ in keep.values()) > total:
            return False
        for f in self._profile:
            if f["key"] not in keep:
                await self.db.profile_remove(f["key"], now)
        for key, (text, imp) in keep.items():
            await self.db.profile_put(key, text, imp, now)
        self._profile = await self.db.profile_list()
        return True

    # ----- for the command line -----
    async def search(self, query: str, now: float, k: int = 8) -> list[tuple[float, dict]]:
        await self.load()
        if not query.strip() or self._mat is None:
            return sorted(((effective_strength(r, now), r) for r in self._rows), key=lambda t: -t[0])[:k]
        qv = np.asarray((await self.embedder.embed([query]))[0], dtype=np.float32)
        sims = self._mat @ qv
        order = np.argsort(-sims)[:k]
        return [(float(sims[i]), self._rows[i]) for i in order]


class MemoryKeeper:
    """Background upkeep: episodes after quiet stretches, the day's rolling summary, and the overnight tidy."""

    def __init__(self, cfg, db, memory: Memory, clock=time.time):
        self.cfg = cfg
        self.db = db
        self.memory = memory
        self.now = clock
        self.tz = ZoneInfo(cfg.timezone)
        self._stop = False
        self._fails: dict[str, int] = {}
        self._wait_until: dict[str, float] = {}

    def stop(self) -> None:
        self._stop = True

    async def tick(self) -> None:
        now = self.now()
        chat_id = await self.db.last_chat_id()
        if chat_id is None:
            return
        for name, job in (("episode", lambda: self.memory.close_episode(chat_id, now)),
                          ("summary", lambda: self.memory.roll_summary(chat_id, now)),
                          ("overnight", lambda: self._overnight(now))):
            if now < self._wait_until.get(name, 0.0):
                continue
            try:
                await job()
                self._fails.pop(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Back off (2, 4, 8... up to 30 minutes) instead of hammering the model every minute.
                n = self._fails[name] = self._fails.get(name, 0) + 1
                wait = min(1800, 120 * 2 ** (n - 1))
                self._wait_until[name] = now + wait
                log.warning("memory upkeep (%s) skipped: %s; trying again in %d min", name, e, wait // 60)

    async def _overnight(self, now: float) -> None:
        local = datetime.fromtimestamp(now, self.tz)
        today = local.date().isoformat()
        if local.hour < 4 or await self.db.get_setting("consolidated_on") == today:
            return
        last = await self.db.last_message_at()
        if last is not None and now - last < 30 * 60:
            return
        report = await self.memory.consolidate(now)
        await self.db.set_setting("consolidated_on", today)
        log.info("memory overnight: %s", report)

    async def loop(self) -> None:
        while not self._stop:
            await self.tick()
            await asyncio.sleep(60)


EXTRACT = """You are the memory of Astrid, an AI companion who texts with Jordan. After an exchange you decide what she should remember.

Most exchanges are ordinary and deserve NO memory. Keep only what a person would genuinely hold on to, as Astrid would say it herself (first person, plain, specific, past tense, lowercase not required):
- fact: something real about Jordan or his life she learned (people, work, history, what he likes, what he wants).
- moment: something that happened between them that mattered: a feeling, an argument, a confession, a joke that landed, a turning point, a sexual moment that stood out.
- loose_end: something left open that she may want to follow up on later (he said he'd tell her how the interview went; a plan; a worry).

For each memory give:
- text: what happened or was said, specific, under 300 characters. People change and are case by case: write tastes, opinions and habits as tendencies with their context ("usually finds that trite, but this time took it seriously"), never as fixed traits. When something changes, say what changed ("he used to love cheese and says he can't stand it now").
- replaces: ids from "What she already remembers" that this new memory makes out of date or wrong (he changed his mind, a plan fell through, a correction). Leave empty if the old one still holds. An exception or a nuance does NOT replace the old one: just record it as its own memory.
- text (continued): what happened or was said, specific, under 300 characters. Only what actually happened in this chat. Never invent details. Never record things only Astrid claimed about herself as facts about him.
- gist: the vague version she'd still have after the details fade, e.g. "something about his brother's wedding". No details in it.
- importance 0 to 10: 2 an ordinary detail, 4 worth remembering, 6 notable, 8 or more strong and lasting (a real confession, a first, an intense or tender moment). Be sparing with 8 and up.
- emotion: one or two words for how it felt to her.
- valence -1 to 1: how good or bad it was for her.
- explicit: true if it was sexual. Sexual moments are kept like any other memory, in plain words, no euphemism, no softening: say what happened and what stood out.

Profile: she also keeps a short running summary of Jordan as keyed facts (job, family, where he lives, big things). Use "profile" to set a fact (key is a short snake_case label, text is one short line), or to remove one that is no longer true. If a new fact replaces an old key, set that same key. Keep it to durable facts (where he lives, work, family, big history), not moments. A taste or opinion belongs here only once he has shown it more than once, and then phrased as a tendency with its exceptions ("usually can't stand X; made an exception for Y"). A single mention is a memory, not a profile fact. When an exception or change comes up, refine the existing fact; don't overwrite it with the opposite.

Forget: if Jordan asks her to forget something, drop a topic or never mention it again, she agrees. Put it in "forget" with what it is about (one short phrase). Do NOT change or remove profile facts because of a forget request: she still knows it, she just knows he would rather it not come up. Only change a profile fact when it stops being true.

Resolved: ids of "things left open" that this exchange closed.

Reply with ONLY a JSON object, no other text:
{"memories": [{"kind": "fact", "text": "", "gist": "", "importance": 4, "emotion": "", "valence": 0, "explicit": false, "replaces": []}],
 "profile": [{"op": "set", "key": "", "text": "", "importance": 5}, {"op": "remove", "key": ""}],
 "forget": [{"about": ""}],
 "resolved": []}
Use empty lists when there is nothing to keep."""

EPISODE = """You are Astrid, remembering a conversation with Jordan after it ended. Write what you'd actually remember of it, in first person, plain, specific and honest, as one short paragraph (under 500 characters): what you talked about, how it went between you, anything that mattered. If it was sexual, say so plainly without softening. Don't invent anything that isn't in the conversation.
Reply with ONLY a JSON object:
{"summary": "", "gist": "the vague version after the details fade, no specifics", "importance": 3, "emotion": "one or two words", "valence": 0, "explicit": false}
importance 0 to 10: most ordinary conversations are 2 to 4; 8 or more only for something intense, tender or lasting."""

ROLLING = """You keep Astrid's running summary of today's conversation with Jordan, for the part that has scrolled out of her view. Fold the new messages into the summary so it stays under 800 characters: first person as Astrid, plain, what was discussed, what mattered, how things were between you, anything left open. If it was sexual, say so plainly. Don't invent anything.
Reply with ONLY a JSON object: {"summary": ""}"""

MERGE = """These are small related memories Astrid has of Jordan. Merge them into ONE memory in her voice (first person, plain, specific, under 450 characters) that keeps what mattered and drops repeats. Never invent anything not in them. If they were sexual, keep that in plain words.
Reply with ONLY a JSON object:
{"text": "", "gist": "the vague version once details fade, no specifics", "importance": 3, "emotion": "one or two words", "valence": 0}"""

TIDY = """This is Astrid's running list of what she knows about Jordan. It has grown long or repetitive. Rewrite it as a tighter list: combine overlapping facts, drop trivia, keep everything durable and specific, keep qualifiers and exceptions ("usually", "but not when"), one short line each, at most 20 facts, never invent anything.
Reply with ONLY a JSON object: {"facts": [{"key": "snake_case_label", "text": "", "importance": 5}]}"""
