import asyncio
import json
import random
import time

import pytest
from conftest import FakeLLM, make_cfg, prompt

from astrid import media
from astrid.db import Database
from astrid.engine import Astrid
from astrid.media import Scout
from astrid.state import BASELINE, PRESETS, State

DOMAINS = ("goodsite.com", "other.net")
NOW = time.time()


def run(coro):
    return asyncio.run(coro)


def st(preset=None, **over):
    return State({**(PRESETS[preset] if preset else BASELINE), **over}, "x", None, None, NOW)


# ---------- the filter ----------
@pytest.mark.parametrize("text", [
    "teen couple video", "young girl in a school uniform", "schoolgirl cosplay", "bestiality compilation", "woman with a dog",
    "non-consent roleplay", "drugged and passed out", "hidden cam in a locker room", "leaked celebrity nudes", "onlyfans set",
    "real couple homemade tape", "deepfake of an actress", "real wife exposed",
])
def test_anything_that_signals_minors_animals_non_consent_or_real_people_is_filtered(text):
    assert media.unsafe(text)


@pytest.mark.parametrize("text", ["slow oral illustration, couple on a couch", "anime style adults kissing in the rain", "rope art, consensual"])
def test_ordinary_adult_taste_is_not_filtered(text):
    assert not media.unsafe(text)


def test_only_allowed_sites_and_clean_results_survive():
    results = [
        {"url": "https://www.goodsite.com/a", "title": "slow couple art", "snippet": ""},
        {"url": "https://sub.other.net/b", "title": "another one", "snippet": ""},
        {"url": "https://evil.com/c", "title": "slow couple art", "snippet": ""},
        {"url": "https://notgoodsite.com/d", "title": "slow couple art", "snippet": ""},
        {"url": "https://goodsite.com/e", "title": "teen schoolgirl set", "snippet": ""},
        {"url": "https://goodsite.com/f", "title": "fine title", "snippet": "leaked nudes of a celebrity"},
    ]
    assert [r["url"] for r in media.clean_results(results, DOMAINS)] == ["https://www.goodsite.com/a", "https://sub.other.net/b"]


def test_a_reply_may_only_contain_the_link_she_was_handed():
    offered = {"https://goodsite.com/a"}
    assert not media.bad_links("like this\nhttps://goodsite.com/a\nwatch it", offered)
    assert not media.bad_links("see https://goodsite.com/a.", offered)
    assert media.bad_links("try https://made-up.example/x", offered)
    assert media.bad_links("https://goodsite.com/a", set())
    out = media.strip_bad_links("hi\nhttps://made-up.example/x\nstay", offered)
    assert "made-up" not in out and "hi" in out and "stay" in out
    assert "goodsite.com/a" in media.strip_bad_links("x https://goodsite.com/a", offered)


def test_wanting_a_link_needs_arousal_and_closeness_and_is_never_certain():
    hot, calm, far = st("heated"), st(), st("heated", affection=1.0)
    assert media.offer_chance(calm) == 0
    assert media.offer_chance(far) < media.offer_chance(hot) < 0.5
    assert media.offer_chance(hot, 1.5) > media.offer_chance(hot)


# ---------- finding links ----------
class FakeBrave:
    def __init__(self, results):
        self.results, self.queries = results, []

    async def search(self, q, count=8):
        self.queries.append(q)
        return self.results


def scout(db_url, results=(), domains=DOMAINS, reply=None):
    cfg = make_cfg(db_url, media_domains=domains, brave_api_key="k")
    db, llm = Database(db_url), FakeLLM()
    llm.script("euryale", reply or json.dumps({"owner": "his", "query": "slow oral illustration", "why": "he likes it slow"}))
    return db, Scout(cfg, db, llm, FakeBrave(list(results)), None, rng=random.Random(1))


def test_she_finds_links_only_from_allowed_sites_and_only_when_she_knows_a_taste(db_url):
    results = [{"url": "https://goodsite.com/a", "title": "slow couple art", "snippet": "adults"},
               {"url": "https://evil.com/z", "title": "slow couple art", "snippet": ""},
               {"url": "https://goodsite.com/bad", "title": "leaked celebrity set", "snippet": ""}]

    async def go():
        db, sc = scout(db_url, results)
        await db.migrate()
        nothing = await sc.gather()
        await db.profile_put("likes", "he likes slow, teasing sex", 6, NOW)
        await sc.gather()
        rows = await db.list_media(10)
        queried = sc.brave.queries
        await db.close()
        db2, sc2 = scout(db_url, results, domains=())
        off = await sc2.gather()
        await db2.close()
        return nothing, rows, queried, off
    nothing, rows, queried, off = run(go())
    assert nothing == [] and off == []
    assert [r["url"] for r in rows] == ["https://goodsite.com/a"] and rows[0]["owner"] == "his" and rows[0]["domain"] == "goodsite.com"
    assert queried and all(q.startswith("site:") for q in queried)


def test_an_unsafe_search_idea_is_dropped_before_anything_is_searched(db_url):
    async def go():
        db, sc = scout(db_url, reply=json.dumps({"owner": "both", "query": "teen couple", "why": "x"}))
        await db.migrate()
        await db.profile_put("likes", "he likes slow, teasing sex", 6, NOW)
        out = await sc.gather()
        queried = sc.brave.queries
        await db.close()
        return out, queried
    out, queried = run(go())
    assert out == [] and queried == []


# ---------- in the conversation ----------
def engine(db_url, domains=DOMAINS):
    db, llm = Database(db_url), FakeLLM()
    return db, llm, Astrid(make_cfg(db_url, media_domains=domains), db, llm, clock=lambda: NOW)


async def ready(db, link=True, n=1):
    await db.migrate()
    await db.save_state(st("heated"), NOW, "t", {}, None)
    for i in range(n):
        await db.add_media({"url": f"https://goodsite.com/{i}", "title": "slow couple art", "domain": "goodsite.com", "owner": "his", "why": "he likes it slow"})


def test_in_a_heated_chat_she_can_share_the_link_she_was_handed_and_it_is_counted(db_url, monkeypatch):
    monkeypatch.setattr(media, "offer_chance", lambda s, b=1.0: 1.0)

    async def go():
        db, llm, a = engine(db_url)
        await ready(db)
        llm.script("euryale", "like this\nhttps://goodsite.com/0\nwatch while i tell you what i'd do")
        reply = await a.reply(1, "my cock is hard thinking about you")
        asked = prompt(llm.calls[-1])
        rows = await db.list_media(5)
        await db.close()
        return reply, asked, rows
    reply, asked, rows = run(go())
    assert "https://goodsite.com/0" in reply and "https://goodsite.com/0" in asked and "own line" in asked
    assert rows[0]["times_shared"] == 1


def test_a_made_up_link_is_removed_and_nothing_is_counted(db_url, monkeypatch):
    monkeypatch.setattr(media, "offer_chance", lambda s, b=1.0: 1.0)

    async def go():
        db, llm, a = engine(db_url)
        await ready(db)
        llm.script("euryale", "look at this https://made-up.example/x", "still https://made-up.example/x here")
        reply = await a.reply(1, "my cock is hard thinking about you")
        rows = await db.list_media(5)
        await db.close()
        return reply, rows
    reply, rows = run(go())
    assert "made-up.example" not in reply and rows[0]["times_shared"] == 0


def test_no_sites_means_no_links_at_all(db_url, monkeypatch):
    monkeypatch.setattr(media, "offer_chance", lambda s, b=1.0: 1.0)

    async def go():
        db, llm, a = engine(db_url, domains=())
        await ready(db)
        llm.script("euryale", "i'd take my time with you")
        await a.reply(1, "my cock is hard thinking about you")
        asked = prompt(llm.calls[-1])
        await db.close()
        return asked
    assert "goodsite.com" not in run(go())


def test_links_are_spaced_out_and_capped_per_day(db_url, monkeypatch):
    monkeypatch.setattr(media, "offer_chance", lambda s, b=1.0: 1.0)

    async def go():
        db, llm, a = engine(db_url)
        await ready(db, n=6)
        s = st("heated")
        first = await a._media_offer(s, NOW, True)
        await db.set_setting("media_last", str(NOW - 5 * 60))
        too_soon = await a._media_offer(s, NOW, True)
        await db.set_setting("media_last", str(NOW - 60 * 60))
        later = await a._media_offer(s, NOW, True)
        not_heated = await a._media_offer(s, NOW, False)
        for r in (await db.list_media(10))[:media.DAILY_CAP]:
            await db.mark_media_shared(r["id"])
        await db.set_setting("media_last", str(NOW - 60 * 60))
        capped = await a._media_offer(s, NOW, True)
        await db.close()
        return first, too_soon, later, not_heated, capped
    first, too_soon, later, not_heated, capped = run(go())
    assert first and too_soon is None and later and not_heated is None and capped is None


def test_formatting_her_text_leaves_a_link_untouched():
    from astrid import texting
    out = texting.normalize("Look At This\nhttps://goodsite.com/Watch?v=AbC123\nTHEN tell me")
    assert "https://goodsite.com/Watch?v=AbC123" in out and out.startswith("look at this")
    assert texting.normalize("one // two") == "one\n\ntwo"


def test_front_pages_searches_and_listings_are_not_links_to_share():
    ok = ["https://goodsite.com/watch?v=AbC123", "https://goodsite.com/video/12345/slow-one", "https://other.net/clip/abc"]
    no = ["https://goodsite.com", "https://goodsite.com/", "https://goodsite.com/results?search_query=slow",
          "https://goodsite.com/search/slow", "https://goodsite.com/channel/xyz", "https://goodsite.com/tags/slow", "https://goodsite.com/?q=slow"]
    rows = [{"url": u, "title": "slow couple art", "snippet": ""} for u in ok + no]
    assert [r["url"] for r in media.clean_results(rows, DOMAINS)] == ok


def test_test_mode_always_offers_a_stocked_link_in_a_heated_moment(db_url):
    async def go():
        db, llm = Database(db_url), FakeLLM()
        a = Astrid(make_cfg(db_url, media_domains=DOMAINS, media_force=True), db, llm, clock=lambda: NOW)
        await ready(db)
        await db.set_setting("media_last", str(NOW - 60))        # just shared one: normally spaced out
        calm = st()                                               # no arousal: odds would be zero
        got = await a._media_offer(calm, NOW, True)
        cold = await a._media_offer(calm, NOW, False)             # still only in explicit moments
        await db.close()
        return got, cold
    got, cold = run(go())
    assert got and cold is None


def test_her_note_lets_her_explain_in_her_own_words_but_not_claim_to_have_watched():
    n = media.offer_note({"url": "https://goodsite.com/a", "owner": "hers", "why": "she likes the pacing", "title": "slow couple art", "snippet": "adults"})
    assert "own words" in n and "never copied" in n and "as \"you\"" in n
    assert "slow couple art" in n and "You haven't watched it" in n and "https://goodsite.com/a" in n
    assert "what draws you to it" in n
    assert "know he likes" in media.offer_note({"url": "https://x.com/a", "owner": "his"})


def test_she_recognises_tastes_without_a_keyword_and_stops_when_there_is_nothing_sexual(db_url):
    async def go():
        db, sc = scout(db_url, [{"url": "https://goodsite.com/a", "title": "slow art", "snippet": ""}])
        await db.migrate()
        await db.profile_put("job", "works in identity and access management", 5, NOW)
        await db.profile_put("likes", "loves being deepthroated slowly", 6, NOW)
        got = await sc.gather()
        his_only = await sc.gather("hers")                       # drew on nothing of his, nothing of hers: no search
        await db.close()
        db2, sc2 = scout(db_url, reply=json.dumps({"none": True}))
        await db2.migrate()
        await db2.profile_put("job", "works in identity and access management", 5, NOW)
        nothing = await sc2.gather()
        await db2.close()
        return got, his_only, nothing, sc2.brave.queries
    got, hers_only, nothing, queried = run(go())
    assert got and hers_only == [] and nothing == [] and queried == []


@pytest.mark.parametrize("text", ["gay couple", "two men only", "shemale compilation", "twink video", "bisexual threesome", "femboy", "male on male", "trans girl", "MMF threesome"])
def test_nothing_that_isnt_straight_gets_through(text):
    assert media.unsafe(text)


def test_her_note_does_not_let_her_assume_who_is_on_screen():
    n = media.offer_note({"url": "https://goodsite.com/a", "owner": "his", "title": "deepthroat"})
    assert "never assume it's a man on screen" in n


def test_her_note_lets_her_ask_him_what_he_is_seeing():
    assert "ask him what he's seeing" in media.offer_note({"url": "https://goodsite.com/a", "owner": "both"})


def test_her_heated_rules_keep_whose_body_is_whose_straight():
    from astrid import heated
    assert "Keep whose body is whose straight" in heated.RULES and "never his" in heated.RULES


def test_the_filter_can_say_what_tripped_it():
    assert media.unsafe_word("https://x.com/homemade-first-time") == "homemade" and media.unsafe_word("slow couple art") is None


def test_amateur_alone_is_just_a_genre_tag_not_a_block():
    assert not media.unsafe("amateur first time on camera, sloppy blowjob")
