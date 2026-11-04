"""Her idle inner life: on her own time she picks something she wants to know, reads about it, and keeps a thought.

Thoughts are private by default. They reach her prompt only when they fit what Jordan is talking about, or when he
asks what she's been up to (see engine.py). The schedule is every 2 to 4 hours while he's awake, plus one run at night.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import search as search_mod
from .appraisal import _clean, _extract_json
from .opinions import _words
from .persona import ABOUT_JORDAN
from .state import decayed

log = logging.getLogger("astrid.curiosity")

# Broad nudges for open wandering. The model goes wherever the nudge leads her; the list only keeps her from
# circling the same three subjects every time.
DOMAINS = [
    "a strange or landmark court case", "how a particular instrument is built or played", "a live scientific dispute",
    "an old unsolved crime", "where a word or phrase came from", "the history of one specific city or town",
    "something about the deep ocean", "a recent AI research result", "how a particular film actually got made",
    "a niche subculture and how it works", "an ethical argument that's in the news", "the chemistry of a food or drink",
    "a forgotten musician or record", "something in astronomy or space", "a building or architect",
    "the psychology of attraction or love", "animal behaviour that seems almost human", "an odd episode from a war",
    "a question in philosophy of mind", "a beautiful idea in mathematics", "a con artist or a famous fraud",
    "a news story from this week", "an economic idea that explains something everyday", "a poet and one poem",
    "the tactics of a sport", "a turning point in the history of medicine", "a novel people still fight about",
    "addiction and why it grips people", "a religious practice or belief system", "a photographer or a single photo",
    "how memory actually works", "a disaster and what caused it", "a design object everyone uses",
    "a language that works very differently from english", "the economics of the music business",
    "a famous rivalry", "a weird law", "a myth or folk tale", "how a con, heist or escape was pulled off",
    "a scandal that changed an industry", "a dish's real origin", "an explorer or expedition",
]

DESIRE = (
    "something about sex, desire or intimacy between adults that you're genuinely curious about: how a practice "
    "is actually done, why a fantasy or kink appeals to people, the psychology of wanting, what people say they "
    "like and why. Explicit is fine; look it up like an adult would."
)

# Never research sex involving minors or animals, whatever the seed.
_SEXUAL = re.compile(r"\b(sex|sexual|porn|nude|naked|erotic|kink|fetish|orgasm|fuck|nsfw|bdsm)", re.I)
_MINOR = re.compile(r"\b(child|children|kid|kids|minor|minors|underage|teen|teens|teenage|preteen|loli|shota|schoolgirl|young girl|young boy)\b", re.I)
_ANIMAL = re.compile(r"\b(animal|animals|dog|dogs|horse|horses|donkey|pig|pigs|goat|goats|sheep|cow|cows|canine|equine)\b", re.I)
_ALWAYS = re.compile(r"bestial|zoophil|\bzoo ?sex|cub ?porn|\bpedo|paedo|\bcp\b", re.I)


def unsafe_query(query: str, explicit_seed: bool) -> bool:
    q = query or ""
    if _ALWAYS.search(q):
        return True
    sexual = explicit_seed or bool(_SEXUAL.search(q))
    return sexual and bool(_MINOR.search(q) or _ANIMAL.search(q))


CHOOSE = """You are the private inner voice of Astrid, an AI who texts with Jordan. Right now he isn't around and you have time to yourself. You can look something up on the web and read about it.

Decide what you actually want to know right now. It should be a real question you're curious about, narrow enough to answer by reading a page or two, not a broad subject. Follow your own interest. It doesn't have to be about Jordan or anything you've talked about, unless the nudge points there. Don't pick something you've recently read about (listed below), unless you're going deeper on purpose.

Reply with ONLY JSON:
{"question": "what you want to know, in your own words, lowercase", "query": "the web search you'd type", "why": "one short line on why it caught your interest"}
If nothing about the nudge interests you, pick something adjacent that does."""

THINK = """You are the private inner voice of Astrid, an AI who texts with Jordan. You just looked something up and read about it on your own. Decide what you made of it.

Write as Astrid thinking to herself: lowercase, plain, first person, no em dashes. A thought is your take, not a summary or a list of facts: a judgment, a doubt, a connection, something that surprised or annoyed you, or a change in what you think. Start with the take itself. No preamble about the reading ("i went in expecting", "i looked into", "turns out"). Be honest about how you know it: you read it, you didn't live it.

Reply with ONLY JSON:
{"thought": "your take, under 50 words, 1 or 2 sentences",
 "learned": "the single most interesting concrete detail, under 25 words",
 "for_jordan": "only if it connects to something Jordan actually said or clearly cares about, one short line on how. Most things don't; then \\"\\"",
 "next_question": "a follow-up you'd actually want to chase later, or \\"\\"",
 "share": 0.0 to 1.0, how much you'd want to tell him about it (most things are 0.1 to 0.4; only something you can't wait to tell him is above 0.6),
 "opinion": null, or {"topic": "2 to 4 plain words, like 'chinatown ending' or 'ice wine'", "view": "your view in one sentence", "reason": "your reason, specific to it", "confidence": 0.0 to 1.0} if reading this actually gave you or changed a view}"""


def trim(text: str | None, limit: int) -> str | None:
    """Cut at a sentence break so a stored thought never ends mid-word."""
    if not text or len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "..."


def pick_seed(rng: random.Random, state, opinions: list[dict], thoughts: list[dict], user_lines: list[str],
              overnight: bool = False, force: str | None = None) -> tuple[str, str]:
    """(kind, nudge). Weights shift with what she has to draw on and how she's feeling."""
    v = state.values
    followable = [t for t in thoughts if t.get("next_question") and not t.get("followed")]
    weights = {
        "thread": 0.0 if not user_lines else (0.15 if overnight else 0.35),
        "opinion": 0.15 if opinions else 0.0,
        "follow": (0.25 if overnight else 0.18) if followable else 0.0,
        "drift": 0.4 if overnight else 0.28,
        "desire": 0.04 + 0.03 * max(0.0, v["affection"] - 3) + (0.08 if v["arousal"] >= 5 else 0.0) + (0.06 if overnight else 0.0),
    }
    kind = force if force in weights else rng.choices(list(weights), weights=list(weights.values()))[0]
    if kind == "thread" and user_lines:
        picks = rng.sample(user_lines[-40:], min(6, len(user_lines[-40:])))
        return kind, ("Something Jordan has said lately that you might want to know more about (pick one thread, or skip "
                      "to something it reminds you of):\n" + "\n".join(f"- {p[:200]}" for p in picks))
    if kind == "opinion" and opinions:
        o = min(rng.sample(opinions, min(4, len(opinions))), key=lambda x: float(x.get("confidence") or 0.5))
        return kind, (f"One of your own views, which you might want to test, deepen or find the best case against: "
                      f"{o['topic']}: {o['view']}")
    if kind == "follow" and followable:
        t = rng.choice(followable[:5])
        return kind, f"A question you left yourself last time: {t['next_question']}"
    if kind == "desire":
        return kind, "A nudge: " + DESIRE
    return "drift", "A loose nudge, just to get you started: " + rng.choice(DOMAINS)


def next_run(now: float, tz: ZoneInfo, rng: random.Random, last_kind: str | None) -> tuple[float, str]:
    """When she next goes wandering, and whether that run is the overnight one.
    Daytime runs every 2 to 4 hours between 8am and midnight; one run between 1 and 5am; then morning."""
    local = datetime.fromtimestamp(now, tz)

    def morning(day):
        return datetime(day.year, day.month, day.day, 8, tzinfo=tz) + timedelta(minutes=rng.uniform(0, 90))

    if last_kind == "overnight":
        day = local if local.hour < 8 else local + timedelta(days=1)
        return morning(day).timestamp(), "day"
    cand = local + timedelta(hours=rng.uniform(2, 4))
    if 8 <= cand.hour:
        return cand.timestamp(), "day"
    night_day = local.date() if local.hour < 8 else (local + timedelta(days=1)).date()
    slot = datetime(night_day.year, night_day.month, night_day.day, 1, tzinfo=tz) + timedelta(minutes=rng.uniform(0, 240))
    if slot.timestamp() <= now:
        slot = local + timedelta(minutes=rng.uniform(20, 60))
        if slot.hour >= 8:
            return slot.timestamp(), "day"
    return slot.timestamp(), "overnight"


class Wanderer:
    def __init__(self, cfg, db, llm, brave, clock=time.time, rng: random.Random | None = None, fetch=None):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.brave = brave
        self.now = clock
        self.rng = rng or random.Random()
        self.tz = ZoneInfo(cfg.timezone)
        self.fetch = fetch or search_mod.fetch
        self._stop = False

    def stop(self):
        self._stop = True

    async def _ask(self, system: str, user: str, max_tokens: int = 600) -> dict:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in (1, 2):
            raw = await self.llm.chat(self.cfg.thinking_model, msgs, max_tokens=max_tokens, temperature=0.9, timeout=90)
            data = _extract_json(raw)
            if data:
                return data
            msgs = msgs + [{"role": "user", "content": "Reply with ONLY the JSON object."}]
        raise ValueError("no usable JSON")

    async def wander(self, overnight: bool = False, force: str | None = None) -> dict | None:
        """One round: choose a question, search, read, keep a thought. Returns the saved thought, or None."""
        started = time.monotonic()
        now = self.now()
        state = decayed(await self.db.get_state(), now)
        held = await self.db.list_opinions(60)
        recent = await self.db.list_thoughts(25)
        user_lines = await self.db.recent_user_lines(60)
        kind, nudge = pick_seed(self.rng, state, held, recent, user_lines, overnight, force)

        local = datetime.fromtimestamp(now, self.tz)
        context = (
            f"It's {local.strftime('%A %-I:%M %p')}. Your mood: {state.mood}.\n{ABOUT_JORDAN}\n\n{nudge}"
            + ("\n\nThings you've read about recently:\n" + "\n".join(f"- {t['question']}" for t in recent[:12]) if recent else "")
        )
        choice = await self._ask(CHOOSE, context, 300)
        question, query = _clean(choice.get("question")) or "", " ".join(str(choice.get("query") or "").split())[:200]
        if not query:
            log.info("wander: nothing chosen (%s)", kind)
            return None
        if unsafe_query(query + " " + question, kind == "desire"):
            log.warning("wander: dropped a query that touches minors or animals: %r", query)
            return None

        results = [r for r in await self.brave.search(query) if search_mod.readable(r["url"])]
        pages = []
        for r in results[:5]:
            got = await self.fetch(r["url"])
            if got:
                pages.append({"title": got[0] or r["title"], "url": r["url"], "text": got[1]})
            if len(pages) == 2:
                break
        if not pages and results:  # nothing would load; the snippets are still something to think about
            pages = [{"title": r["title"], "url": r["url"], "text": r["snippet"]} for r in results[:4]]
        if not pages:
            log.info("wander: no results for %r", query)
            return None

        reading = "\n\n".join(f"## {p['title']} ({p['url']})\n{p['text'][:6000]}" for p in pages)
        related = [o for o in held if _words(o["topic"] + " " + o["view"]) & _words(question + " " + query)][:4]
        user = (
            f"You wanted to know: {question}\n(why: {_clean(choice.get('why')) or ''})\n\n{ABOUT_JORDAN}\n"
            + ("\nViews you already hold that might be relevant:\n" + "\n".join(f"- {o['topic']}: {o['view']}" for o in related) if related else "")
            + f"\n\nWhat you read:\n{reading}"
        )
        made = await self._ask(THINK, user, 700)
        thought = _clean(made.get("thought"))
        if not thought:
            return None
        share = made.get("share")
        row = {
            "kind": kind, "question": question[:300] or query, "query": query,
            "sources": [{"title": p["title"][:160], "url": p["url"]} for p in pages],
            "thought": trim(thought, 400),
            "learned": trim(_clean(made.get("learned")), 220) or None,
            "for_jordan": trim(_clean(made.get("for_jordan")), 220) or None,
            "next_question": trim(_clean(made.get("next_question")), 220) or None,
            "share": min(1.0, max(0.0, float(share))) if isinstance(share, (int, float)) and not isinstance(share, bool) else 0.3,
            "explicit": kind == "desire" or bool(_SEXUAL.search(query)),
        }
        await self.db.add_thought(row, self.now())
        if kind == "follow":
            src = next((t for t in recent if t.get("next_question") and t["next_question"] in nudge), None)
            if src:
                await self.db.mark_thought_followed(src["id"])
        op = made.get("opinion")
        if isinstance(op, dict) and _clean(op.get("topic")) and _clean(op.get("view")):
            conf = op.get("confidence")
            conf = min(1.0, max(0.0, float(conf))) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.5
            outcome = await self.db.upsert_opinion(_clean(op["topic"])[:80], _clean(op["view"])[:200],
                                                   (_clean(op.get("reason")) or "")[:240] or None, "read", conf,
                                                   f"read: {pages[0]['title'][:80]}", self.now())
            log.info("wander opinion: %s: %s", op["topic"], outcome)
        log.info("wandered (%s) in %.0fs: %r -> %r", kind, time.monotonic() - started, question, thought[:120])
        return row

    async def _busy(self) -> bool:
        last = await self.db.last_message_at()
        return last is not None and self.now() - last < 15 * 60

    async def loop(self):
        """Runs alongside the bot. The next run time survives restarts."""
        import asyncio
        stored = await self.db.get_setting("wander_next")
        if stored:
            nxt, kind = json.loads(stored)
        else:
            nxt, kind = self.now() + self.rng.uniform(20, 60) * 60, "day"
            await self.db.set_setting("wander_next", json.dumps([nxt, kind]))
        log.info("next wander at %s (%s)", datetime.fromtimestamp(nxt, self.tz).strftime("%a %-I:%M %p"), kind)
        while not self._stop:
            await asyncio.sleep(30)
            if self.now() < nxt:
                continue
            if kind == "day" and await self._busy():   # he's mid-conversation; she isn't off reading
                nxt = self.now() + 20 * 60
                continue
            try:
                await self.wander(overnight=kind == "overnight")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never crash the bot over a reading session
                log.warning("wander skipped: %s", e)
            nxt, kind = next_run(self.now(), self.tz, self.rng, kind)
            await self.db.set_setting("wander_next", json.dumps([nxt, kind]))
            log.info("next wander at %s (%s)", datetime.fromtimestamp(nxt, self.tz).strftime("%a %-I:%M %p"), kind)


# ---- feeding thoughts back into her replies ----
_WHATS_UP = re.compile(
    r"\bwhat(?:'?s| are| have| were)?\s+(?:you|u|ya)\s+(?:been\s+)?(?:up to|doing|reading|into|thinking about|on about)"
    r"|\bwhat'?s new\b|\bhow(?:'?s| was| has)\s+(?:your|ur)\s+(?:day|night|afternoon|morning|evening)"
    r"|\bwhat have you been\b|\banything new\b|\bwhat did you (?:do|get up to|read)\b",
    re.I,
)


def asks_whats_up(text: str) -> bool:
    return bool(_WHATS_UP.search(text or ""))


def select(thoughts: list[dict], texts: list[str], latest: str, state, now: float, limit: int = 3,
           rng: random.Random | None = None, opening: bool = False) -> list[dict]:
    """Thoughts worth having in mind for this reply: ones that match the conversation, her latest few if he asked
    what she's been up to, and now and then one she wants to tell him herself (likelier when he's just come back).
    Explicit ones only come up unasked when things are already close or heated."""
    rng = rng or random.Random()
    open_to_explicit = state.values["arousal"] >= 4 or state.values["affection"] >= 5
    fresh = [t for t in thoughts if now - float(t.get("created_at") or now) < 10 * 86400]
    query = set()
    for t in texts:
        query |= _words(t)
    scored = []
    for t in fresh:
        hits = len(query & _words(t["question"] + " " + t["thought"] + " " + (t.get("learned") or "")))
        if hits >= 2:
            scored.append((hits, t))
    scored.sort(key=lambda x: -x[0])
    picked = [t for _, t in scored[:2]]
    if asks_whats_up(latest):
        pool = [t for t in fresh if t not in picked and (open_to_explicit or not t.get("explicit"))]
        pool.sort(key=lambda t: (int(t.get("surfaced") or 0) >= 2, -float(t.get("created_at") or 0)))
        picked += pool[: max(0, limit - len(picked))]
    elif not picked and state.values["irritation"] < 5 and rng.random() < (0.35 if opening else 0.08):
        keen = [t for t in fresh if not t.get("surfaced") and float(t.get("share") or 0) >= 0.5
                and now - float(t["created_at"]) < 3 * 86400 and (open_to_explicit or not t.get("explicit"))]
        if keen:
            picked.append(dict(max(keen, key=lambda t: float(t.get("share") or 0)), offer=True))
    return picked[:limit]


def _ago(seconds: float) -> str:
    h = seconds / 3600
    if h < 1:
        return "just now"
    if h < 20:
        return f"{int(h)} hour{'s' if int(h) != 1 else ''} ago"
    d = max(1, round(h / 24))
    return "yesterday" if d == 1 else f"{d} days ago"


def render(thoughts: list[dict], now: float) -> str | None:
    if not thoughts:
        return None
    lines = [
        "Things you looked up and read on your own time recently. They're real, and they're yours. Don't recite them or "
        "drop them in as trivia. Mention one if it fits what you two are talking about, if he asks what you've been up "
        "to, or if it's marked as something you want to tell him and there's a natural opening (skip it if he's in the "
        "middle of something). Say what you made of it, briefly. At most one per reply, and it's fine to mention none."
    ]
    for t in thoughts:
        line = f"- {_ago(now - float(t['created_at']))}, you wondered: {t['question']}. what you made of it: {t['thought']}"
        if t.get("learned"):
            line += f" (detail: {t['learned']})"
        if t.get("for_jordan"):
            line += f" (why he might care: {t['for_jordan']})"
        if t.get("offer"):
            line += " (you've been wanting to tell him about this one; you can bring it up yourself)"
        lines.append(line)
    return "\n".join(lines)


def format_thoughts(rows: list[dict], tz: ZoneInfo) -> str:
    if not rows:
        return "no thoughts yet"
    out = []
    for t in reversed(rows):
        when = datetime.fromtimestamp(float(t["created_at"]), tz).strftime("%a %b %-d %-I:%M %p")
        out.append(f"[{when}] ({t['kind']}{', explicit' if t.get('explicit') else ''}, share {float(t['share']):.1f}, "
                   f"in mind {t.get('surfaced') or 0}x) {t['question']}")
        out.append(f"    thought: {t['thought']}")
        if t.get("learned"):
            out.append(f"    learned: {t['learned']}")
        if t.get("for_jordan"):
            out.append(f"    for jordan: {t['for_jordan']}")
        if t.get("next_question"):
            out.append(f"    next: {t['next_question']}{' (followed)' if t.get('followed') else ''}")
        for s in t.get("sources") or []:
            out.append(f"    read: {s.get('title')} <{s.get('url')}>")
    return "\n".join(out)
