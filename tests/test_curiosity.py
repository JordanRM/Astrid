import asyncio
import json
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from astrid import curiosity, search
from astrid.config import Config
from astrid.db import Database
from astrid.state import BASELINE, State
from tests.conftest import FakeLLM

TZ = ZoneInfo("America/Toronto")


def cfg():
    return Config(database_url="x", openrouter_api_key="k", telegram_token="t", interactor_model="fast", thinking_model="think")


def st(**kw):
    v = dict(BASELINE); v.update(kw)
    return State(v, "neutral", None, None, 1.0)


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


def test_extract_text_keeps_paragraphs_and_drops_menus():
    page = "<html><head><title>Ice wine</title><script>x=1</script></head><body><nav><li>Home menu link item here ok</li></nav>" \
           "<p>Grapes are picked frozen at minus eight degrees, usually at night, by hand.</p><footer><p>copyright footer text here ok</p></footer></body></html>"
    title, text = search.extract_text(page)
    assert title == "Ice wine" and "minus eight" in text and "menu" not in text and "copyright" not in text


def test_readable_skips_video_and_social():
    assert not search.readable("https://www.youtube.com/watch?v=1")
    assert search.readable("https://en.wikipedia.org/wiki/Ice_wine")


def test_brave_asks_with_safesearch_off(monkeypatch):
    seen = {}

    class R:
        status_code = 200
        def json(self):
            return {"web": {"results": [{"title": "<strong>A</strong>", "url": "https://a.com", "description": "d &amp; e"}]}}

    class S:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.update(params=params, headers=headers)
            return R()
    monkeypatch.setattr(search, "session", lambda: S())
    out = asyncio.run(search.Brave("key").search("q"))
    assert seen["params"]["safesearch"] == "off" and seen["headers"]["X-Subscription-Token"] == "key"
    assert out == [{"title": "A", "url": "https://a.com", "snippet": "d & e"}]


def test_minor_guard():
    assert curiosity.unsafe_query("teen fantasies", True)
    assert curiosity.unsafe_query("teen sex", False)
    assert not curiosity.unsafe_query("teen pop history", False)
    assert not curiosity.unsafe_query("why people like being tied up", True)
    assert curiosity.unsafe_query("sex with a dog", False) and curiosity.unsafe_query("horse", True)
    assert curiosity.unsafe_query("zoophilia history", False)
    assert not curiosity.unsafe_query("how sled dogs are trained", False) and not curiosity.unsafe_query("doggy style tips", True)


def test_desire_gets_likelier_when_close_and_wanting():
    def share(state):
        rng = random.Random(1)
        return sum(curiosity.pick_seed(rng, state, [], [], ["hi"])[0] == "desire" for _ in range(3000)) / 3000
    assert share(st(affection=7, arousal=6)) > share(st(affection=1, arousal=0)) * 2


def test_schedule_runs_by_day_once_at_night_then_morning():
    rng = random.Random(3)
    t, kind = curiosity.next_run(at(2026, 11, 3, 12), TZ, rng, "day")
    assert kind == "day" and 2 * 3600 <= t - at(2026, 11, 3, 12) <= 4 * 3600
    t, kind = curiosity.next_run(at(2026, 11, 3, 23), TZ, rng, "day")
    local = datetime.fromtimestamp(t, TZ)
    assert kind == "overnight" and local.day == 4 and 1 <= local.hour < 5
    t, kind = curiosity.next_run(t, TZ, rng, "overnight")
    local = datetime.fromtimestamp(t, TZ)
    assert kind == "day" and local.day == 4 and 8 <= local.hour < 10


def test_select_matches_conversation_and_whats_up():
    now = at(2026, 11, 3, 20)
    thoughts = [
        {"id": 1, "created_at": now - 3600, "question": "why towne's chinatown ending got changed", "thought": "polanski was right about the ending", "explicit": False},
        {"id": 2, "created_at": now - 7200, "question": "why people like being tied up", "thought": "the relief part surprised me", "explicit": True},
        {"id": 3, "created_at": now - 20 * 86400, "question": "old one", "thought": "stale", "explicit": False},
    ]
    picked = curiosity.select(thoughts, ["chinatown ending was perfect, polanski knew"], "chinatown ending was perfect, polanski knew", st(), now)
    assert [t["id"] for t in picked] == [1]
    assert [t["id"] for t in curiosity.select(thoughts, ["hey"], "what have you been up to", st(), now)] == [1]
    assert [t["id"] for t in curiosity.select(thoughts, ["hey"], "what are you up to", st(arousal=6), now)] == [1, 2]
    assert curiosity.select(thoughts, ["hey"], "nice weather", st(), now) == []
    assert "don't recite" in curiosity.render(picked, now).lower()


def test_whats_up_phrasings():
    for t in ["what are you up to", "whatcha been doing? what have you been doing", "how was your day", "what'd you do today? what did you do today", "anything new"]:
        assert curiosity.asks_whats_up(t), t
    assert not curiosity.asks_whats_up("what are you saying")


class FakeBrave:
    def __init__(self, results):
        self.results, self.queries = results, []

    async def search(self, q, count=8):
        self.queries.append(q)
        return self.results


def test_wander_saves_a_thought_and_an_opinion(db_url):
    async def go():
        db = Database(db_url)
        await db.migrate()
        llm = FakeLLM()
        llm.script("think",
                   json.dumps({"question": "how do they make ice wine", "query": "ice wine harvest temperature", "why": "his winemaking"}),
                   json.dumps({"thought": "it's absurd — and i respect it", "learned": "picked at minus eight", "for_jordan": "his wine",
                               "next_question": "can you fake it with a freezer", "share": 0.6,
                               "opinion": {"topic": "ice wine", "view": "worth the madness", "reason": "the cold does what nothing else can", "confidence": 0.6}}))
        brave = FakeBrave([{"title": "YT", "url": "https://youtube.com/x", "snippet": ""},
                           {"title": "Ice wine", "url": "https://wine.example/ice", "snippet": "s"}])

        async def fetch(url):
            return ("Ice wine", "Grapes freeze on the vine. " * 30)
        w = curiosity.Wanderer(cfg(), db, llm, brave, clock=lambda: at(2026, 11, 3, 15), rng=random.Random(2), fetch=fetch)
        row = await w.wander(force="drift")
        thoughts = await db.list_thoughts()
        ops = await db.list_opinions()
        await db.close()
        return row, thoughts, ops, llm
    row, thoughts, ops, llm = asyncio.run(go())
    assert row["thought"] == "it's absurd, and i respect it"
    assert thoughts[0]["sources"] == [{"title": "Ice wine", "url": "https://wine.example/ice"}]
    assert ops[0]["topic"] == "ice wine" and ops[0]["source"] == "read"
    assert "Grapes freeze" in llm.calls[1]["messages"][1]["content"]


def test_wander_drops_queries_about_minors(db_url):
    async def go():
        db = Database(db_url)
        await db.migrate()
        llm = FakeLLM()
        llm.script("think", json.dumps({"question": "x", "query": "teen kink", "why": ""}))
        brave = FakeBrave([])
        w = curiosity.Wanderer(cfg(), db, llm, brave, clock=lambda: at(2026, 11, 3, 15), rng=random.Random(2))
        row = await w.wander(force="desire")
        await db.close()
        return row, brave
    row, brave = asyncio.run(go())
    assert row is None and brave.queries == []


def test_reset_clears_thoughts_and_schedule(db_url):
    async def go():
        db = Database(db_url)
        await db.migrate()
        await db.add_thought({"kind": "drift", "question": "q", "thought": "t"}, 100.0)
        await db.set_setting("wander_next", "[1, \"day\"]")
        await db.reset_all(200.0)
        out = await db.list_thoughts(), await db.get_setting("wander_next")
        await db.close()
        return out
    assert asyncio.run(go()) == ([], None)


def test_she_sometimes_brings_one_up_herself_mostly_when_he_comes_back():
    now = at(2026, 11, 3, 20)
    keen = {"id": 7, "created_at": now - 3600, "question": "how sled dogs pick a leader", "thought": "it's not the strongest one", "share": 0.8, "explicit": False, "surfaced": 0}
    hot = {"id": 8, "created_at": now - 3600, "question": "x", "thought": "y", "share": 0.9, "explicit": True, "surfaced": 0}

    def rate(opening, state, items):
        rng = random.Random(5)
        return sum(bool(curiosity.select(items, ["hey"], "hey", state, now, rng=rng, opening=opening)) for _ in range(2000)) / 2000
    assert 0.25 < rate(True, st(), [keen]) < 0.45 and rate(False, st(), [keen]) < 0.15
    assert rate(True, st(), [hot]) == 0 and rate(True, st(arousal=6), [hot]) > 0.25
    assert rate(True, st(irritation=6), [keen]) == 0
    picked = curiosity.select([keen], ["hey"], "hey", st(), now, rng=random.Random(0), opening=True) or \
        next(p for p in (curiosity.select([keen], ["hey"], "hey", st(), now, rng=random.Random(i), opening=True) for i in range(50)) if p)
    assert "bring it up yourself" in curiosity.render(picked, now)


def test_trim_cuts_at_a_sentence():
    t = "the folklore does more work than the science. the sightings cluster on paths and roads, not deep swamp, which i"
    assert curiosity.trim(t, 90) == "the folklore does more work than the science."
    assert curiosity.trim("short", 90) == "short"
    assert "no preamble" in curiosity.THINK.lower() and "under 50 words" in curiosity.THINK
