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
                   r"influencer|tiktoker|instagram model|amateur|homemade|real (?:couple|wife|girlfriend)|\bex-?gf\b|ex-?girlfriend", re.I)

MAX_UNSHARED = 8
SPACING_MIN = 15       # minutes between links
DAILY_CAP = 3

WANT = """You are the private inner voice of Astrid, an AI who texts with Jordan. You're thinking about something to share with him in an adult moment: a link to something you both might like. Decide what to search for.

Pick one of:
- "his": something that matches a taste he's told you about.
- "hers": something you like yourself, from what you've read or what you've felt, that he may not know about yet.
- "both": where the two overlap.
Write a short search (3 to 8 plain words) describing the kind of thing, never names of people. Illustrated or generated content is fine and preferred. Adults only, always consensual.

Reply with ONLY JSON:
{"owner": "his" | "hers" | "both", "query": "the search terms", "why": "one short line in your own words on why this one"}"""


def unsafe(text: str) -> bool:
    t = text or ""
    return bool(_ALWAYS.search(t) or _MINOR.search(t) or _ANIMAL.search(t) or _NONCON.search(t) or _REAL.search(t))


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
        "his": "It matches something he's told you he likes.",
        "hers": "It's something you like yourself, and he may not know that about you yet. If it fits, say so and ask what he thinks.",
        "both": "It's something you think you'd both like.",
    }.get(row.get("owner"), "")
    return (f"You've got a link you want to share right now: {row['url']} ({row.get('title') or 'no title'}). {own} "
            f"Your reason: {row.get('why') or 'you thought of him'}. Put the link on its own line, with a few words in your own voice around it "
            "(like \"like this\", or what it is, or why you'd want him to see it), then carry on with what you're doing. You haven't watched it "
            "yourself; you found it. Share only this link, never any other address.")


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
        his += [f["text"] for f in await self.db.profile_list() if _SEXUAL.search(f["text"])][:6]
        hers = [f"{t['thought']}" for t in await self.db.list_thoughts(25) if t.get("explicit") and t.get("thought")][:6]
        hers += [f"{o['topic']}: {o['view']}" for o in await self.db.list_opinions(60) if _SEXUAL.search(o["topic"] + " " + o["view"])][:4]
        return his, hers

    async def gather(self) -> list[dict]:
        if not self.enabled:
            return []
        if len([m for m in await self.db.list_media(40, unshared_only=True)]) >= MAX_UNSHARED:
            return []
        his, hers = await self.tastes()
        if not his and not hers:
            return []                                          # she doesn't know what she or he likes yet
        user = ("What he's told you he likes:\n" + ("\n".join(f"- {t}" for t in his) or "- nothing yet")
                + "\n\nWhat you like yourself:\n" + ("\n".join(f"- {t}" for t in hers) or "- nothing yet"))
        try:
            raw = await self.llm.chat(self.cfg.interactor_model, [{"role": "system", "content": WANT}, {"role": "user", "content": user}],
                                      temperature=0.9, max_tokens=300, timeout=60, extra={"reasoning": {"enabled": False}})
            data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
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
                    saved.append({"id": mid, "url": r["url"], "title": r["title"], "owner": owner})
        log.info("media: kept %d link%s for %r (%s)", len(saved), "" if len(saved) == 1 else "s", query, owner)
        return saved
