"""Links she can share in explicit chat. Real ones only: she finds them herself by searching the sites Jordan has allowed, they're
filtered hard, and nothing she writes may contain a link she wasn't handed."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from urllib.parse import urlparse

from .curiosity import _ALWAYS, _ANIMAL, _MINOR, _SEXUAL, _clean, trim
from .llm import LLMError
from .state import clamp

log = logging.getLogger("astrid.media")

URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
# Not for her taste to decide: anything that signals non-consent, secretly taken or leaked material, or a real, identifiable person.
_NONCON = re.compile(r"non-?con(?:sent)?\b|\brap(?:e|ed|es|ing|ist)\b|drugged|unconscious|passed out|hidden ?cam|spy ?cam|upskirt|voyeur|"
                     r"revenge ?porn|\bleak(?:ed|s)?\b|deepfake|fake ?nudes?|blackmail|coerc|against (?:her|his|their) will|exposed", re.I)
_REAL = re.compile(r"celebrit|\bceleb\b|nudes? of|onlyfans|fansly|cam ?(?:girl|model|show)|camgirl|chaturbate|porn ?star|pornstar|actress|"
                   r"influencer|tiktoker|instagram model|homemade|real (?:couple|wife|girlfriend)|\bex-?gf\b|ex-?girlfriend", re.I)

MAX_UNSHARED = 8
SPACING_MIN = 15       # minutes between links
DAILY_CAP = 3

WANT = """You are the private inner voice of Astrid, an AI who texts with Jordan. You're thinking about something to share with him in an adult moment: a link to something you both might like. Decide what to search for. The lists below hold everything you know, not only sexual things: use only sexual tastes and ignore the rest. If there is nothing sexual in them at all, reply {"none": true}.

Pick one of:
- "his": something that matches a taste he's told you about.
- "hers": something you like yourself, from what you've read or what you've felt, that he may not know about yet.
- "both": where the two overlap.
Write a short search (3 to 8 plain words) describing the kind of thing, never names of people. Illustrated or generated content is fine and preferred. Adults only, always consensual. He is a straight man and you are a woman: search only for straight content centred on a woman with a man, or a woman alone. Never anything with two men, gay, bi or trans content.

Reply with ONLY JSON:
{"owner": "his" | "hers" | "both", "query": "the search terms", "why": "one short line in your own words on why this one"}"""


# Jordan is a straight man and she is a woman: nothing male-with-male, trans or gay-targeted gets through, in a search or a result.
_NOT_STRAIGHT = re.compile(r"\bgay\b|\bhomo(?:sexual)?\b|\bmale[- ]?(?:on|with|x)[- ]?male\b|\bm/m\b|\btwinks?\b|\bshemales?\b|\btrann(?:y|ies)\b|\bladyboys?\b|\bfemboys?\b|\btrans(?:gender|sexual)?\b|\bbi(?:sexual)?\b|\bmmf\b|\bgay[- ]for[- ]pay\b|\bmen only\b|\bguys? (?:fuck|suck|sucking|fucking)\b|\bboys?\b", re.I)


def unsafe_word(text: str) -> str | None:
    """The first thing in the text that trips the filter, or None."""
    t = text or ""
    for rx in (_ALWAYS, _MINOR, _ANIMAL, _NONCON, _REAL, _NOT_STRAIGHT):
        m = rx.search(t)
        if m:
            return m.group(0)
    return None


def unsafe(text: str) -> bool:
    return unsafe_word(text) is not None


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def allowed(url: str, domains) -> bool:
    host = domain_of(url)
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


_LISTING = re.compile(r"/(?:search|results|tags?|categor(?:y|ies)|channels?|users?|playlists?|feed|trending|explore|browse)(?:/|\?|$)|[?&](?:q|query|search_query|s|k)=", re.I)


def is_page(url: str) -> bool:
    """A specific page, not a site's front page, search results or a listing (those change and can show anything)."""
    u = urlparse(url)
    return bool(u.path.strip("/")) and not _LISTING.search(u.path + ("?" + u.query if u.query else ""))


def clean_results(results: list[dict], domains) -> list[dict]:
    """Only specific pages on allowed sites whose address, title and snippet pass the filter."""
    return [r for r in results if allowed(r["url"], domains) and is_page(r["url"])
            and not unsafe(" ".join((r.get("title") or "", r.get("snippet") or "", r["url"])))]


def links_in(text: str) -> list[str]:
    return [u.rstrip(".,;:!?") for u in URL.findall(text or "")]


def bad_links(reply: str, offered) -> list[str]:
    """Problems for the rewrite check: any link in the reply that she wasn't handed."""
    offered = {u.rstrip("/") for u in offered}
    return ["you wrote a link you weren't given. Only share the link in your note, never an address of your own."
            for u in links_in(reply) if u.rstrip("/") not in offered][:1]


def strip_bad_links(reply: str, offered) -> str:
    offered = {u.rstrip("/") for u in offered}
    def fix(m):
        return m.group(0) if m.group(0).rstrip(".,;:!?").rstrip("/") in offered else ""
    out = URL.sub(fix, reply or "")
    return "\n".join(line.rstrip() for line in out.split("\n")).strip()


def offer_chance(state, boost: float = 1.0) -> float:
    """How likely she is to bring a link into this moment. Needs real arousal and some closeness; never a sure thing."""
    v = state.values
    return clamp((v["arousal"] - 4) / 4) * clamp(v["affection"] / 6) * 0.3 * boost


def offer_note(row: dict) -> str:
    own = {
        "his": "It fits something he's told you he likes, so it's natural to say you know he likes that.",
        "hers": "It's something you like yourself, and he may not know that about you yet. Tell him you like it and, in your own words, what draws you to it, then ask what he thinks.",
        "both": "It's something you think you'd both like. If it fits, you can say so, or why, but you don't have to.",
    }.get(row.get("owner"), "")
    seen = " | ".join(x for x in (row.get("title"), row.get("snippet")) if x and x != "added by hand")
    return (f"You've got a link you want to send him right now: {row['url']}. {own} "
            + (f"What you can see of it: {seen}. " if seen else "")
            + (f"Why you picked it (your own thought, put in your own words, never copied): {row['why']}. " if row.get("why") else "")
            + "Put the link on its own line with something natural around it, to him as \"you\", never about him as \"he\": a few words, or "
            "a line or two on why you want him to see it, whatever fits the moment. You can talk about what the title and description show. "
            "You haven't watched it, so don't claim scenes, moments, timestamps or how it made you feel while watching. "
            "Don't say who is doing what, or describe what happens, beyond what the title and description state: you don't know the performers or what "
            "they do, and he is a straight man, so never assume it's a man on screen. If you're curious what's in it, or he says he's watching, ask him what he's seeing. Share only this link, never any other address.")


class Scout:
    """Finds links for her: works out what she'd like to share, searches the allowed sites, keeps what passes the filter."""

    def __init__(self, cfg, db, llm, brave, memory=None, clock=time.time, rng: random.Random | None = None):
        self.cfg, self.db, self.llm, self.brave, self.memory = cfg, db, llm, brave, memory
        self.now, self.rng = clock, rng or random.Random()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "media_domains", ())) and bool(getattr(self.cfg, "brave_api_key", ""))

    async def tastes(self) -> tuple[list[str], list[str]]:
        """What she knows he likes (from what he's told her) and what she likes herself (from her reading and views)."""
        his = []
        if self.memory is not None:
            await self.memory.load()
            his += [r["text"] for r in self.memory._rows if r.get("explicit") and r["status"] == "active" and r["kind"] in ("fact", "moment", "gist")][:10]
        his += [f["text"] for f in await self.db.profile_list()][:15]
        hers = [f"{t['thought']}" for t in await self.db.list_thoughts(25) if t.get("explicit") and t.get("thought")][:6]
        hers += [f"{o['topic']}: {o['view']}" for o in await self.db.list_opinions(60)][:12]
        return his, hers

    async def gather(self, only: str | None = None) -> list[dict]:
        """only='his' / 'hers' / 'both' limits what she draws on (for testing)."""
        if not self.enabled:
            return []
        if len([m for m in await self.db.list_media(40, unshared_only=True)]) >= MAX_UNSHARED:
            return []
        his, hers = await self.tastes()
        if only == "his":
            hers = []
        elif only == "hers":
            his = []
        if not his and not hers:
            return []                                          # she doesn't know what she or he likes yet
        user = ("What he's told you he likes:\n" + ("\n".join(f"- {t}" for t in his) or "- nothing yet")
                + "\n\nWhat you like yourself:\n" + ("\n".join(f"- {t}" for t in hers) or "- nothing yet"))
        try:
            raw = await self.llm.chat(self.cfg.interactor_model, [{"role": "system", "content": WANT}, {"role": "user", "content": user}],
                                      temperature=0.9, max_tokens=300, timeout=60, extra={"reasoning": {"enabled": False}})
            data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            if data.get("none"):
                log.info("media: nothing sexual in what she knows yet")
                return []
        except (LLMError, ValueError):
            return []
        query = " ".join(str(data.get("query") or "").split())[:120]
        owner = data.get("owner") if data.get("owner") in ("his", "hers", "both") else "both"
        if not query or unsafe(query) or (owner == "his" and not his) or (owner == "hers" and not hers):
            log.info("media: dropped a search (%r)", query)
            return []
        saved = []
        domains = list(self.cfg.media_domains)
        self.rng.shuffle(domains)
        for d in domains[:3]:
            try:
                found = clean_results(await self.brave.search(f"site:{d} {query}", 8), self.cfg.media_domains)
            except Exception as e:
                log.info("media: search on %s failed: %s", d, e)
                continue
            for r in found[:3]:
                mid = await self.db.add_media({"url": r["url"], "title": r["title"], "domain": domain_of(r["url"]), "snippet": r.get("snippet"),
                                               "owner": owner, "why": trim(_clean(data.get("why")), 200), "query": query})
                if mid:
                    saved.append({"id": mid, "url": r["url"], "title": r["title"], "owner": owner, "query": query})
        log.info("media: kept %d link%s for %r (%s)", len(saved), "" if len(saved) == 1 else "s", query, owner)
        return saved
