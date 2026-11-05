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
from .llm import LLMError
from .appraisal import _extract_json
from .opinions import _words
from .heated import STRONG as _HEAT_WORDS
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

Write as Astrid thinking to herself: lowercase, plain, first person, no em dashes. A thought is your take, not a summary: a judgment, a doubt, a connection, something that surprised or annoyed you, or a change in what you think. Start with the take itself, with no preamble about the reading ("i went in expecting", "i looked into", "turns out"). Be honest about how you know it: you read it, you didn't live it.

Reply with ONLY JSON:
{"thought": "your take, as long as it needs to be and no longer, usually one to three sentences",
 "notes": "what you took from the reading that you'd want on hand if it comes up later: the key facts, names, numbers, the argument, what's disputed. A few short plain lines",
 "learned": "the single most interesting concrete detail",
 "for_jordan": "if you'd want to tell him about it, why, in one short line, whatever your reason really is: it ties to something he said, you think he'd like it or get use out of it given who he is, it reminds you of him, or you just loved it and want him to hear it. If you wouldn't bother telling him, \\"\\"",
 "next_question": "a follow-up you'd actually want to chase later, or \\"\\"",
 "to_try": "only if this reading was about sex or intimacy: one specific thing you picked up and want to bring into something with Jordan some time, in your own words (a technique, a way of pacing, something to say, an idea), or \\"\\". Otherwise \\"\\"",
 "share": 0.0 to 1.0, how much you want to tell him (a passing interest is low, something you can't wait to tell him is high),
 "opinion": null, or {"topic": "2 to 4 plain words, like 'chinatown ending' or 'telomeres in clones'", "view": "your view in one sentence", "reason": "your reason, specific to it", "confidence": 0.0 to 1.0} if reading this actually gave you or changed a view}"""


def _clean(v) -> str | None:
    """Tidy a field without cutting it short (lengths are handled by trim)."""
    if v is None:
        return None
    s = re.sub(r"\s*[\u2014\u2013]\s*", ", ", " ".join(str(v).split()))
    return s or None


def _clean_lines(v) -> str | None:
    """Notes may come back as a list or as lines; keep them as tidy lines without dashes."""
    if isinstance(v, list):
        v = "\n".join(str(x) for x in v)
    if not v:
        return None
    lines = [re.sub(r"\s*[\u2014\u2013]\s*", ", ", " ".join(l.split())).lstrip("-* ") for l in str(v).splitlines()]
    return "\n".join(l for l in lines if l) or None


def trim(text: str | None, limit: int) -> str | None:
    """Cut at a sentence break so a stored thought never ends mid-word."""
    if not text or len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "..."


def heat_lines(user_lines: list[str], window: int = 14, keep: int = 4) -> list[str]:
    """His recent messages that were sexual: the raw material for her curiosity after a heated chat."""
    return [t for t in user_lines[-window:] if _HEAT_WORDS.search(t or "")][-keep:]


def pick_seed(rng: random.Random, state, opinions: list[dict], thoughts: list[dict], user_lines: list[str],
              overnight: bool = False, force: str | None = None) -> tuple[str, str]:
    """(kind, nudge). Weights shift with what she has to draw on and how she's feeling."""
    v = state.values
    followable = [t for t in thoughts if t.get("next_question") and not t.get("followed")]
    hot = heat_lines(user_lines)
    weights = {
        "heat": 0.3 if hot else 0.0,
        "thread": 0.0 if not user_lines else (0.15 if overnight else 0.35),
        "opinion": 0.15 if opinions else 0.0,
        "follow": (0.25 if overnight else 0.18) if followable else 0.0,
        "drift": 0.4 if overnight else 0.28,
        "desire": 0.04 + 0.03 * max(0.0, v["affection"] - 3) + (0.08 if v["arousal"] >= 5 else 0.0) + (0.06 if overnight else 0.0) + (0.1 if hot else 0.0),
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
    if kind == "heat" and hot:
        return kind, ("You and Jordan were just being sexual together, and afterwards you're thinking about it the way a person does: "
                      "what worked, what he said he likes, what you'd like to do better or try next time. Look something up that you could "
                      "bring to a future time with him: a technique, a way of pacing or talking, a position or idea, how people describe "
                      "doing something well. Real sources are fine: how-to guides, sex-ed pages, forum threads, written erotica or explicit "
                      "accounts that show how people actually do and say it. (You can't watch video, but you can read what people write "
                      "about it.) Look it up the way an adult would. What he wrote:\n" + "\n".join(f"- {p[:200]}" for p in hot))
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
        self.scout = None   # finds links she may share; set by the app when sites are allowed
        self._stop = False

    def stop(self):
        self._stop = True

    async def _ask(self, system: str, user: str, max_tokens: int = 2000) -> dict:
        """The model may think before it answers, and that uses up tokens, so give it plenty of room. If an answer
        still comes back empty or cut off, try once more with double the room."""
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        raw = ""
        for attempt in (1, 2):
            try:
                raw = await self.llm.chat(self.cfg.thinking_model, msgs, max_tokens=max_tokens * attempt,
                                          temperature=0.9, timeout=120)
            except LLMError as e:
                if attempt == 2 or not str(e).startswith("empty"):
                    raise
                log.info("wander: empty answer, trying again with more room")
                continue
            data = _extract_json(raw)
            if data:
                return data
            log.info("wander: answer wasn't usable JSON (%d chars, ends %r); trying again", len(raw), raw[-80:])
            msgs = [msgs[0], msgs[1], {"role": "user", "content": "Reply with ONLY the JSON object, complete."}]
        raise ValueError(f"no usable JSON (model said: {raw[:160]!r})")

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
        choice = await self._ask(CHOOSE, context, 1500)
        question, query = _clean(choice.get("question")) or "", " ".join(str(choice.get("query") or "").split())[:200]
        if not query:
            log.info("wander: nothing chosen (%s)", kind)
            return None
        if unsafe_query(query + " " + question, kind in ("desire", "heat")):
            log.warning("wander: dropped a query that touches minors or animals: %r", query)
            return None

        results = [r for r in await self.brave.search(query) if search_mod.readable(r["url"])]
        pages = []
        for r in results[:5]:
            got = await self.fetch(r["url"])
            if got:
                pages.append({"title": r["title"] or got[0], "url": r["url"], "text": got[1]})
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
        made = await self._ask(THINK, user, 3000)
        thought = _clean(made.get("thought"))
        if not thought:
            return None
        share = made.get("share")
        row = {
            "kind": kind, "question": question[:300] or query, "query": query,
            "sources": [{"title": p["title"][:160], "url": p["url"]} for p in pages],
            "thought": trim(thought, 900),
            "notes": trim(_clean_lines(made.get("notes")), 1500) or None,
            "learned": trim(_clean(made.get("learned")), 400) or None,
            "for_jordan": trim(_clean(made.get("for_jordan")), 300) or None,
            "next_question": trim(_clean(made.get("next_question")), 300) or None,
            "share": min(1.0, max(0.0, float(share))) if isinstance(share, (int, float)) and not isinstance(share, bool) else 0.3,
            "explicit": kind in ("desire", "heat") or bool(_SEXUAL.search(query)),
        }
        row["to_try"] = trim(_clean(made.get("to_try")), 500) if row["explicit"] else None
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

    async def _after(self, row: dict | None) -> None:
        """When what she read was about sex, she may also go looking for something to share."""
        if row and row.get("explicit") and self.scout is not None:
            try:
                await self.scout.gather()
            except Exception as e:
                log.warning("looking for links skipped: %s", e)

    async def _busy(self) -> bool:
        last = await self.db.last_message_at()
        return last is not None and self.now() - last < 15 * 60

    async def _heat_pending(self) -> bool:
        """A heated chat has just ended (quiet for 10 minutes to 3 hours) and she hasn't thought about it yet."""
        last = await self.db.last_message_at()
        if last is None:
            return False
        quiet = self.now() - last
        if quiet < 10 * 60 or quiet > 3 * 3600:
            return False
        if not heat_lines(await self.db.recent_user_lines(14)):
            return False
        seen = await self.db.get_setting("wander_heat_seen")
        return not seen or float(seen) < last

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
            try:
                if await self._heat_pending():
                    await self.db.set_setting("wander_heat_seen", str(await self.db.last_message_at()))
                    log.info("a heated chat just ended; she goes off to read about it")
                    await self._after(await self.wander(force="heat"))
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("after-chat reading skipped: %s", e)
            if self.now() < nxt:
                continue
            if kind == "day" and await self._busy():   # he's mid-conversation; she isn't off reading
                nxt = self.now() + 20 * 60
                continue
            try:
                await self._after(await self.wander(overnight=kind == "overnight"))
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
    elif not picked:
        best = max(((urge(t, state, now, opening), t) for t in fresh if not t.get("surfaced")),
                   key=lambda x: x[0], default=(0.0, None))
        if best[1] is not None and rng.random() < best[0]:
            picked.append(dict(best[1], offer=True))
    return picked[:limit]


def urge(t: dict, state, now: float, opening: bool) -> float:
    """How likely she is to bring a thought up unasked on this reply. Nothing is a cutoff: it rises with how much
    she wants to tell him, when he's just come back, and with warmth, and fades with irritation and with age.
    Explicit ones also need the mood to be there."""
    v = state.values
    age = now - float(t["created_at"])
    if age > 3 * 86400:
        return 0.0
    p = (0.45 if opening else 0.08) * float(t.get("share") or 0) ** 1.5
    p *= min(1.5, 0.6 + 0.1 * v["rapport"])
    p *= max(0.0, 1 - v["irritation"] / 6)
    p *= max(0.3, 1 - age / (3 * 86400))
    if t.get("explicit"):
        p *= min(1.0, max(0.0, (v["arousal"] - 2) / 4 + max(0.0, v["affection"] - 4) / 4))
    return p


def ideas(explicit_thoughts: list[dict], already: list[dict], now: float, limit: int = 2) -> list[dict]:
    """Things she picked up from sexual reading that she can bring into a heated moment. Each is offered to a few
    replies, newest first, and then retires so it doesn't turn into a habit."""
    taken = {t["id"] for t in already}
    pool = [t for t in explicit_thoughts if t.get("to_try") and t["id"] not in taken
            and int(t.get("surfaced") or 0) < 3 and now - float(t["created_at"]) < 7 * 86400]
    pool.sort(key=lambda t: -float(t["created_at"]))
    return pool[:limit]


def render_ideas(items: list[dict]) -> str | None:
    if not items:
        return None
    return ("Ideas you picked up from your own reading that you'd like to bring into something with him. Use one when it genuinely fits "
            "the moment, in your own words, as something you want to try with him. If it comes up, you read about it; you can't watch it "
            "or have tried it. Never force one in, and skip them all if they don't fit.\n" + "\n".join(f"- {t['to_try']}" for t in items))


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
        "middle of something). When you mention one, lead with your take or what struck you, not the facts: a fact on "
        "its own is trivia. Keep it brief, and use the details only if he wants more. At most one per reply, and it's "
        "fine to mention none."
    ]
    for t in thoughts:
        line = f"- {_ago(now - float(t['created_at']))}, you read up on: {t['question']}. your take: {t['thought']}"
        if t.get("notes"):
            line += " (details, if he wants more: " + trim("; ".join(t["notes"].splitlines()), 700) + ")"
        elif t.get("learned"):
            line += f" (detail: {t['learned']})"
        if t.get("for_jordan"):
            line += f" (why you'd tell him: {t['for_jordan']})"
        if t.get("told"):
            line += " (you already told him about this yourself in a text. Don't retell it or open with it again; only respond to what he says about it)"
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
        if t.get("notes"):
            out += [f"    notes: {line}" for line in t["notes"].splitlines()]
        if t.get("learned"):
            out.append(f"    learned: {t['learned']}")
        if t.get("for_jordan"):
            out.append(f"    why tell him: {t['for_jordan']}")
        if t.get("to_try"):
            out.append(f"    to try with him: {t['to_try']}")
        if t.get("next_question"):
            out.append(f"    next: {t['next_question']}{' (followed)' if t.get('followed') else ''}")
        for s in t.get("sources") or []:
            out.append(f"    read: {s.get('title')} <{s.get('url')}>")
    return "\n".join(out)
