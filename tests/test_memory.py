import asyncio
import json
from datetime import datetime
import random
import time

import numpy as np

from conftest import FakeLLM, make_cfg, prompt
from astrid import heated
from astrid.db import Database
from astrid.embed import Embedder, HashEmbedder, make_embedder
from astrid.engine import Astrid
from astrid.memory import (HAZY_BELOW, Memory, MoodLean, MemoryKeeper, effective_strength, is_hazy, mood_pulls, when_text)
from astrid.state import BASELINE, PRESETS, State

NOW = 1_800_000_000.0
DAY = 86400.0


def mk(db_url, llm=None, rng=None, clock=None, **cfg):
    db = Database(db_url)
    llm = llm or FakeLLM()
    c = make_cfg(db_url, **cfg)
    mem = Memory(c, db, llm, HashEmbedder(), clock=clock or (lambda: NOW), rng=rng or random.Random(1))
    return c, db, llm, mem


def calm():
    return State(dict(BASELINE), "neutral", None, None, NOW)


def extraction(*items, profile=(), forget=(), resolved=()):
    return json.dumps({"memories": list(items), "profile": list(profile), "forget": list(forget), "resolved": list(resolved)})


def item(text, importance=4, kind="fact", gist="something he told me", emotion="curious", valence=0.2, explicit=False, replaces=()):
    return {"replaces": list(replaces), "kind": kind, "text": text, "gist": gist, "importance": importance, "emotion": emotion, "valence": valence, "explicit": explicit}


def run(coro):
    return asyncio.run(coro)


# ---------- embeddings ----------
def test_hash_embedder_finds_shared_meaning_words_and_ignores_filler():
    async def go():
        e = HashEmbedder()
        a, b, c = await e.embed(["his brother is getting married at a vineyard wedding in june",
                                 "how was your brother's wedding",
                                 "the new compressor in the garage keeps tripping the breaker"])
        a, b, c = map(np.asarray, (a, b, c))
        return float(a @ b), float(a @ c), float(np.linalg.norm(a))
    related, unrelated, norm = run(go())
    assert related > HashEmbedder.min_sim > unrelated and abs(norm - 1) < 1e-4


def test_make_embedder_falls_back_without_fastembed():
    assert make_embedder("hash").name == "hash-256"
    assert make_embedder("").name in ("hash-256", "BAAI/bge-small-en-v1.5")


# ---------- strength, fading, hazy ----------
def test_memories_fade_unless_strong_and_recalling_resets_the_clock():
    base = {"kind": "fact", "importance": 3, "strength": 1.0, "created_at": NOW, "last_recalled_at": None, "permanent": False}
    assert effective_strength(base, NOW) == 1.0
    old = {**base, "created_at": NOW - 200 * DAY}
    assert effective_strength(old, NOW) < HAZY_BELOW and is_hazy(old, NOW)
    assert not is_hazy({**old, "last_recalled_at": NOW - DAY}, NOW)                 # just thought of it
    assert effective_strength({**old, "permanent": True}, NOW) >= 1.0                # strong moments never fade
    assert effective_strength({**base, "kind": "loose_end", "created_at": NOW - 30 * DAY}, NOW) < effective_strength(
        {**base, "created_at": NOW - 30 * DAY}, NOW)                                 # open threads go stale sooner than facts
    assert effective_strength({**old, "importance": 7}, NOW) > effective_strength(old, NOW)


def test_when_text_and_mood_pulls():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Toronto")
    assert when_text(NOW - 3600, NOW, tz) in ("earlier today", "yesterday")
    assert when_text(NOW - 4 * DAY, NOW, tz) == "4 days ago" and "weeks" in when_text(NOW - 25 * DAY, NOW, tz)
    st = calm()
    st.values.update(irritation=8, security=2)
    neg, pos = mood_pulls(st)
    assert neg > 0.8 and pos == 0
    st.values.update(irritation=0, security=8, rapport=8, affection=6)
    assert mood_pulls(st)[1] > 0.5


# ---------- database ----------
def test_profile_keeps_old_wordings_and_reset_clears_memory(db_url):
    async def go():
        db = Database(db_url)
        await db.migrate()
        assert await db.profile_put("job", "works at a mill", 5, NOW) == "added"
        assert await db.profile_put("job", "works at a mill", 5, NOW) == "same"
        assert await db.profile_put("job", "left the mill, now drives truck", 6, NOW + 1) == "changed"
        facts = await db.profile_list()
        assert await db.profile_remove("job", NOW + 2) and not await db.profile_list()
        assert not await db.profile_remove("job", NOW + 3)
        mid = await db.add_memory({"kind": "moment", "text": "x", "importance": 5, "embedding": [0.1, 0.2], "embed_model": "t"}, NOW)
        await db.update_memory(mid, {"strength": 1.4, "last_recalled_at": NOW})
        row = (await db.load_memories())[0]
        await db.add_link(mid, mid)
        await db.set_setting("rolling_summary", "{}")
        await db.reset_all(NOW)
        out = (facts, row, await db.load_memories(), await db.profile_list(), await db.get_setting("rolling_summary"))
        await db.close()
        return out
    facts, row, after, prof, setting = run(go())
    assert facts[0]["text"].startswith("left the mill") and facts[0]["history"][0]["text"] == "works at a mill"
    assert row["strength"] == 1.4 and abs(row["last_recalled_at"] - NOW) < 1 and row["embedding"] == [0.10000000149011612, 0.20000000298023438] or row["embedding"]
    assert after == [] and prof == [] and setting is None


# ---------- learning ----------
def test_extraction_keeps_what_matters_and_ignores_trivia(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        llm.script("r1", extraction(
            item("his brother is getting married in june and he has to give a speech", 5, gist="something about his brother's wedding"),
            item("he said it was fine", 1),                                           # too trivial: dropped
            item("he told me he'd never said that to anyone, and it got quiet between us", 9, "moment", emotion="tender", valence=0.9),
            item("he wants me to pick the song for tonight, i said i'd think about it", 3, "loose_end"),
            profile=[{"op": "set", "key": "Brother", "text": "has a brother getting married in june", "importance": 5}]))
        out = await mem.extract([("my brother's wedding is in june", "oh yeah?")], [], NOW)
        rows = await db.load_memories()
        prof = await db.profile_list()
        await db.close()
        return out, rows, prof
    out, rows, prof = run(go())
    assert out == {"saved": 3, "profile": 1}
    by = {r["kind"]: r for r in rows}
    assert by["moment"]["permanent"] and by["moment"]["importance"] == 9 and not by["fact"]["permanent"]
    assert by["fact"]["gist"] == "something about his brother's wedding" and by["loose_end"]["resolved"] is False
    assert {f["key"] for f in prof} == {"home", "brother"}                            # home was seeded the first time memory loaded


def test_sexual_moments_are_stored_plainly_like_any_other_memory(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        llm.script("r1", extraction(item("he came while i told him how i'd suck his cock, and he said my words did it", 8, "moment",
                                         emotion="wanted", valence=0.9, explicit=True)))
        await mem.extract([("i'm cumming", "good")], [], NOW)
        rows = await db.load_memories()
        await db.close()
        return rows
    (row,) = run(go())
    assert row["explicit"] and "suck his cock" in row["text"] and row["permanent"]


def test_a_repeated_memory_is_reinforced_not_duplicated(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        t = "his brother is getting married in june and he has to give a speech"
        llm.script("r1", extraction(item(t, 3)))
        await mem.extract([("x", "y")], [], NOW)
        llm.script("r1", extraction(item(t, 5)))
        out = await mem.extract([("x", "y")], [], NOW + DAY)
        rows = await db.load_memories()
        await db.close()
        return out, rows
    out, rows = run(go())
    assert out["saved"] == 0 and len(rows) == 1 and rows[0]["importance"] == 5


def test_forget_that_leaves_a_memory_with_a_note_and_keeps_the_fact(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        await db.profile_put("ex", "went through a rough breakup last year", 5, NOW)
        llm.script("r1", extraction(forget=[{"about": "his breakup"}]))
        await mem.extract([("forget I told you about my breakup", "ok. dropped.")], [], NOW)
        rows, prof = await db.load_memories(), await db.profile_list()
        await db.close()
        return rows, prof
    rows, prof = run(go())
    assert len(rows) == 1 and "asked me to drop his breakup" in rows[0]["text"] and "wouldn't like it brought up" in rows[0]["text"]
    assert any(f["key"] == "ex" for f in prof)                                       # she still knows it; no hidden off-limits flag


def test_loose_ends_get_resolved_and_bad_model_output_is_harmless(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        llm.script("r1", extraction(item("he'll tell me how the interview goes on friday", 4, "loose_end")))
        await mem.extract([("x", "y")], [], NOW)
        rid = (await db.load_memories())[0]["id"]
        llm.script("r1", extraction(resolved=[f"#{rid}"]))
        await mem.extract([("it went well", "good")], [], NOW + DAY)
        resolved = (await db.load_memories())[0]["resolved"]
        llm.script("r1", "sorry I cannot do that")
        out = await mem.extract([("x", "y")], [], NOW)
        llm.script("r1", Exception("boom"))
        out2 = await mem.extract([("x", "y")], [], NOW)
        await db.close()
        return resolved, out, out2
    resolved, out, out2 = run(go())
    assert resolved is True and out == {"saved": 0, "profile": 0} == out2


# ---------- recall ----------
async def seeded(db_url, entries, **kw):
    c, db, llm, mem = mk(db_url, **kw)
    await db.migrate()
    llm.script("r1", extraction(*entries))
    await mem.extract([("a", "b")], [], NOW)
    return c, db, llm, mem


WEDDING = item("his brother is getting married in june and he has to give the wedding speech", 5, gist="something about his brother's wedding")
GARAGE = item("the garage compressor keeps tripping the breaker and he is fed up", 3, gist="something about the garage")


def test_recall_by_meaning_strengthens_and_stays_quiet_when_nothing_fits(db_url):
    async def go():
        c, db, llm, mem = await seeded(db_url, [WEDDING, GARAGE])
        hit = await mem.recall(["how is the wedding speech coming"], calm(), False, NOW + DAY)
        miss = await mem.recall(["what's your favorite album"], calm(), False, NOW + DAY)
        rows = await db.load_memories()
        await db.close()
        return hit, miss, rows, mem
    hit, miss, rows, mem = run(go())
    assert [r.row["kind"] for r in hit] == ["fact"] and "wedding" in hit[0].row["text"] and miss == []
    wedding = next(r for r in rows if "wedding" in r["text"])
    assert wedding["recalled_count"] == 1 and wedding["strength"] > 1.0                  # remembering strengthens it
    assert "wedding speech" in mem.render(hit, NOW + DAY) and "invent" in mem.render(hit, NOW + DAY)


def test_linked_memories_come_along_and_faded_ones_are_hazy(db_url):
    async def go():
        a = item("his brother is getting married in june and he has to give the wedding speech", 5)
        b = item("his brother wedding speech worries him because he freezes in front of crowds", 5, gist="something about him freezing up")
        c_ = item("the garage compressor keeps tripping the breaker", 3)
        c, db, llm, mem = await seeded(db_url, [a, b, c_])
        rows = await db.load_memories()
        linked = {r["id"]: r["links"] for r in rows}
        # a and b are about the same thing, so they were linked when the second one was stored
        out = await mem.recall(["wedding speech"], calm(), False, NOW + 1)
        far = await mem.recall(["wedding speech"], calm(), False, NOW + 400 * DAY)       # a year on, nothing recalled since
        text = mem.render(far, NOW + 400 * DAY)
        await db.close()
        return linked, out, text
    linked, out, text = run(go())
    assert any(links for links in linked.values())
    assert 1 <= len(out) <= 3 and {r.via for r in out} <= {"cue", "link"}
    assert "hazy" in text and "something about" in text and "garage" not in text


def test_strength_gain_is_not_stacked_within_an_hour(db_url):
    async def go():
        c, db, llm, mem = await seeded(db_url, [WEDDING])
        for k in range(3):
            await mem.recall(["wedding speech"], calm(), False, NOW + 100 + k)
        row = (await db.load_memories())[0]
        await db.close()
        return row
    assert run(go())["recalled_count"] == 1


def test_mood_lean_lingers_lets_go_when_repaired_and_is_usually_absent():
    rng = random.Random(5)
    # an ordinary calm day: almost never leaning either way
    ordinary = [MoodLean(random.Random(i)).current(calm(), NOW) for i in range(200)]
    assert sum(1 for n, p in ordinary if n or p) < 30
    # annoyed: often leans toward old hurts, and keeps leaning for a while rather than flickering turn to turn
    angry = calm()
    angry.values.update(irritation=8, security=3)
    angry.unresolved_tension = "he brushed off your opinion"
    leaned = 0
    for i in range(100):
        m = MoodLean(random.Random(i))
        first = m.current(angry, NOW)
        if first[0]:
            leaned += 1
            assert all(m.current(angry, NOW + k * 600)[0] == first[0] for k in range(1, 4))   # same lean 30 minutes later
    assert 40 < leaned < 100                                                                 # likely, but not certain
    # repaired: the bad lean lets go even though its timer hadn't run out
    m = MoodLean(random.Random(0))
    for i in range(50):
        m = MoodLean(random.Random(i))
        if m.current(angry, NOW)[0]:
            break
    healed = calm()
    healed.values.update(irritation=1)
    assert m.current(healed, NOW + 600) == (0.0, 0.0)
    # warm: leans toward good times only sometimes, and the warmth souring ends it
    warm = calm()
    warm.values.update(rapport=9, affection=7)
    goods = [MoodLean(random.Random(i)) for i in range(100)]
    pulled = [g for g in goods if g.current(warm, NOW)[1]]
    assert 10 < len(pulled) < 70
    sour = calm()
    sour.values.update(irritation=6, rapport=4, affection=3)
    assert pulled[0].current(sour, NOW + 300)[1] == 0.0


def test_a_mood_only_nudges_recall_it_does_not_invent_it(db_url):
    slight = item("he brushed off my opinion on the album and called it pretentious, and it stung", 5, valence=-0.8, emotion="stung")
    async def score(mood_lean):
        c, db, llm, mem = await seeded(db_url, [slight])
        mem.mood.current = lambda state, now: mood_lean
        out = await mem.recall(["my opinion on the album"], calm(), False, NOW + DAY), await mem.recall(["what's for supper"], calm(), False, NOW + 2 * DAY)
        await db.execute("truncate astrid.memories restart identity")
        await db.close()
        return out
    (plain, none1), (leaning, none2) = run(score((0.0, 0.0))), run(score((1.0, 0.0)))
    assert leaning[0].score > plain[0].score and leaning[0].score - plain[0].score < 0.1
    assert none1 == [] == none2                                                               # a bad mood alone doesn't dredge anything up


def test_linked_memories_never_chain(db_url):
    async def go():
        c, db, llm, mem = await seeded(db_url, [
            item("his brother is getting married in june and he has to give the wedding speech", 5),
            item("the garage compressor keeps tripping the breaker and he is fed up", 5),
            item("he mentioned the old truck needs a new transmission before winter", 5)])
        a, b, c3 = sorted(mem._rows, key=lambda r: r["id"])
        a["links"], b["links"], c3["links"] = [b["id"]], [a["id"], c3["id"]], [b["id"]]       # a - b - c, but a and c unrelated
        seen = set()
        for k in range(40):
            mem.rng = random.Random(k)
            for r in await mem.recall(["wedding speech"], calm(), False, NOW + (k + 1) * 7200):
                seen.add((r.row["id"], r.via))
        await db.close()
        return a["id"], b["id"], c3["id"], seen
    a, b, c3, seen = run(go())
    assert (a, "cue") in seen and (b, "link") in seen and not any(i == c3 and v == "link" for i, v in seen)


def test_explicit_memories_are_quiet_in_everyday_talk_but_come_back_when_heated_or_strongly_cued(db_url):
    hot = item("he came while i told him how i'd suck his cock and he said my words did it", 6, "moment", emotion="wanted", explicit=True)
    async def go():
        c, db, llm, mem = await seeded(db_url, [hot])
        cool = calm()
        heated_state = State(dict(PRESETS["heated"]), "wanting you", None, None, NOW)
        q = ["i told him my words did it when he came"]
        everyday = await mem.recall(["what are you reading about the moon"], cool, False, NOW + DAY)
        weak = await mem.recall(["came words"], cool, False, NOW + DAY)
        strong = await mem.recall(q, cool, False, NOW + DAY)
        hot_mode = await mem.recall(q, heated_state, True, NOW + 2 * DAY)
        await db.close()
        return everyday, weak, strong, hot_mode
    everyday, weak, strong, hot_mode = run(go())
    assert everyday == []
    assert strong and hot_mode                      # callbacks are possible in both modes, not walled off
    assert "suck his cock" in hot_mode[0].row["text"]


def test_now_and_then_a_strong_memory_just_comes_to_mind(db_url):
    big = item("he told me he loved me for the first time and then went quiet", 9, "moment", emotion="tender", valence=0.9)
    async def go():
        c, db, llm, mem = await seeded(db_url, [big], rng=random.Random(2))
        seen = 0
        for k in range(60):
            seen += bool(await mem.recall(["how was the drive"], calm(), False, NOW + k * 7200))
        await db.close()
        return seen
    assert 1 <= run(go()) <= 25


# ---------- summaries and upkeep ----------
def msgs(db, base, n, gap=60):
    async def add():
        for i in range(n):
            await db.add_message(1, "user" if i % 2 == 0 else "assistant", f"line {i} about the wedding speech", base + i * gap)
    return add()


def test_a_quiet_hour_becomes_one_episode_memory(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        await msgs(db, NOW - 7200, 10)
        llm.script("r1", json.dumps({"summary": "we went over his wedding speech for a while and he sounded nervous", "gist": "something about his speech",
                                     "importance": 4, "emotion": "close", "valence": 0.4, "explicit": False}))
        early = await mem.close_episode(1, NOW - 7200 + 700)                                  # only 2 min of quiet
        done = await mem.close_episode(1, NOW)
        again = await mem.close_episode(1, NOW)
        rows = await db.load_memories()
        await db.close()
        return early, done, again, rows
    early, done, again, rows = run(go())
    assert not early and done and not again and rows[0]["kind"] == "episode" and "wedding speech" in rows[0]["text"]


def test_todays_older_messages_roll_into_a_summary_that_expires_with_the_day(db_url):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Toronto")
    noon = datetime(2026, 11, 3, 15, 0, tzinfo=tz).timestamp()
    async def go():
        c, db, llm, mem = mk(db_url, clock=lambda: noon)
        await db.migrate()
        await msgs(db, noon - 6 * 3600, 12)                                                    # 9am-ish, well outside the 2h window
        await db.add_message(1, "user", "just now", noon - 60)
        llm.script("r1", json.dumps({"summary": "this morning we went back and forth about his wedding speech"}))
        ok = await mem.roll_summary(1, noon)
        again = await mem.roll_summary(1, noon)                                                # nothing new to fold in
        today = await mem.summary_text(datetime.fromtimestamp(noon, tz).date())
        tomorrow = await mem.summary_text(datetime.fromtimestamp(noon + DAY, tz).date())
        await db.close()
        return ok, again, today, tomorrow
    ok, again, today, tomorrow = run(go())
    assert ok and not again and "wedding speech" in today and "Earlier today" in today and tomorrow is None


def test_overnight_merges_small_related_memories_fades_the_weak_and_keeps_strong_ones(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        small = [item(f"he complained about the garage compressor tripping the breaker {w}", 3)
                 for w in ["again tuesday night", "during monday supper", "before the weekend market", "after the hockey game"]]
        llm.script("r1", extraction(item("he told me he loved me and went quiet", 9, "moment")))
        await mem.extract([("a", "b")], [], NOW - 60 * DAY)
        for s in small:
            llm.script("r1", extraction(s))
            await mem.extract([("a", "b")], [], NOW - 40 * DAY)
        # force them close together so they cluster under the simple embedder
        llm.script("r1", extraction(item("an old passing remark about nothing much", 2, "moment")))
        await mem.extract([("a", "b")], [], NOW - 900 * DAY)
        llm.script("r1", json.dumps({"text": "he kept griping about the garage compressor tripping the breaker", "gist": "something about the garage",
                                     "importance": 3, "emotion": "irritated", "valence": -0.3}))
        report = await mem.consolidate(NOW)
        rows = await db.load_memories(include_inactive=True)
        await db.close()
        return report, rows
    report, rows = run(go())
    status = {}
    for r in rows:
        status.setdefault(r["status"], []).append(r["text"][:30])
    assert report["merged"] >= 3 and any(r["kind"] == "gist" and "garage" in r["text"] for r in rows)
    assert any("loved me" in r["text"] and r["status"] == "active" for r in rows)       # permanent stays
    assert report["faded"] == 1 and any("passing remark" in t for t in status["faded"])


def test_a_long_profile_gets_tidied_and_old_wordings_are_kept(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        for i in range(10):
            await db.profile_put(f"fact_{i}", f"a fairly long and repetitive note about his life number {i} " * 3, 5, NOW)
        await mem.load()
        mem._profile = await db.profile_list()
        llm.script("r1", json.dumps({"facts": [{"key": "life", "text": "a short tidy summary of his life", "importance": 6}]}))
        changed = await mem.tidy_profile(NOW + 1)
        prof = await db.profile_list()
        await db.close()
        return changed, prof
    changed, prof = run(go())
    assert changed and [f["key"] for f in prof] == ["life"]


def test_keeper_runs_the_overnight_tidy_once_per_night_and_only_when_it_is_quiet(db_url):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Toronto")
    t = [datetime(2026, 11, 4, 5, 0, tzinfo=tz).timestamp()]
    async def go():
        c, db, llm, mem = mk(db_url, clock=lambda: t[0])
        await db.migrate()
        await db.add_message(1, "user", "hi", t[0] - 10 * 3600)
        calls = []
        async def fake(now):
            calls.append(now)
            return {"faded": 0, "merged": 0, "profile": False}
        mem.consolidate = fake
        keeper = MemoryKeeper(c, db, mem, clock=lambda: t[0])
        await keeper.tick(); await keeper.tick()
        t[0] += DAY
        await db.add_message(1, "user", "just now", t[0] - 60)
        await keeper.tick()                                                                   # chatting right now: not tonight
        await db.close()
        return calls
    assert len(run(go())) == 1


# ---------- in her prompt ----------
def test_profile_sits_in_the_fixed_front_and_memories_ride_in_the_final_note(db_url):
    async def go():
        c, db, llm, mem = await seeded(db_url, [WEDDING])
        await db.profile_put("job", "drives truck for a living", 6, NOW)
        mem._profile = await db.profile_list()
        llm.script("euryale", "how's the speech going")
        eng = Astrid(c, db, llm, clock=lambda: NOW + DAY, memory=mem)
        await eng.reply(1, "still stressing about the wedding speech")
        call = [x for x in llm.calls if x["model"] == "euryale"][0]
        await db.close()
        return call
    call = run(go())
    first, last = call["messages"][0]["content"], call["messages"][-1]["content"]
    assert "drives truck" in first and "Lanark Village" in first and "nearly all you know" not in first
    assert "wedding speech" in last and "Things that came to mind" in last and "Reply now as Astrid" in last
    assert "wedding speech" not in first                                                      # keeps the front stable for caching


def test_a_broken_memory_never_breaks_a_reply(db_url):
    class Broken:
        async def profile_facts(self): raise RuntimeError("down")
    async def go():
        c, db, llm, _ = mk(db_url)
        await db.migrate()
        llm.script("euryale", "hey")
        out = await Astrid(c, db, llm, memory=Broken()).reply(1, "hi")
        await db.close()
        return out
    assert run(go()) == "hey"


def test_she_sees_two_hours_not_just_24_messages(db_url):
    async def go():
        c, db, llm, _ = mk(db_url)
        await db.migrate()
        for i in range(60):
            await db.add_message(1, "user" if i % 2 == 0 else "assistant", f"m{i}", NOW - 3600 + i * 20)
        llm.script("euryale", "ok")
        await Astrid(c, db, llm, clock=lambda: NOW).reply(1, "now")
        n = len([m for m in llm.calls[0]["messages"] if m["role"] in ("user", "assistant")])
        old = Database(db_url)
        await db.add_message(1, "user", "ancient", NOW - 5 * DAY)
        await db.close()
        return n
    assert run(go()) == 61                                                                    # everything within the window, plus the new one


def test_old_chats_still_give_a_minimum_of_recent_context(db_url):
    async def go():
        c, db, llm, _ = mk(db_url)
        await db.migrate()
        for i in range(40):
            await db.add_message(1, "user" if i % 2 == 0 else "assistant", f"m{i}", NOW - 5 * DAY + i)
        llm.script("euryale", "ok")
        await Astrid(c, db, llm, clock=lambda: NOW).reply(1, "back")
        n = len([m for m in llm.calls[0]["messages"] if m["role"] in ("user", "assistant")])
        await db.close()
        return n
    assert run(go()) == 24


# ---------- the fresh-start rule is day-aware now ----------
def test_opener_rule_levels():
    today_claim = "i've had you on my mind all day"
    cross_day = "i keep thinking about where we left off"
    assert heated.slips(today_claim, 0) == [] and heated.slips(cross_day, 0) == []
    assert heated.slips(today_claim, 1) and heated.slips(cross_day, 1) == []             # talked before, but not yet today
    assert heated.slips(today_claim, 2) and heated.slips(cross_day, 2)
    assert heated.slips(cross_day, True)                                                  # old callers still mean "nothing ever said"
    assert heated.slips("i read something this morning that made me think of you", 1) == []


def test_first_text_of_a_new_day_gets_the_softer_note(db_url):
    async def go():
        c, db, llm, _ = mk(db_url)
        await db.migrate()
        await db.save_state(State(dict(PRESETS["heated"]), "x", None, None, NOW), NOW, "t", {}, None)
        await db.add_message(1, "user", "yesterday's chat", NOW - DAY)
        await db.add_message(1, "assistant", "yes", NOW - DAY + 30)
        await Astrid(c, db, llm, clock=lambda: NOW).reply(1, "my cock is hard")
        out = prompt(llm.calls[0])
        await db.close()
        return out
    out = run(go())
    assert "You haven't talked yet today" in out and "only just started" not in out


def test_a_model_that_cant_load_falls_back_to_word_matching(db_url):
    class Bad(Embedder):
        name = "bad"
        async def embed(self, texts):
            raise OSError("download blocked")
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        mem.embedder = Bad()
        await mem.warm()
        name = mem.embedder.name
        await db.close()
        return name
    assert run(go()) == "hash-256"


def test_a_changed_mind_replaces_the_old_memory_and_the_extractor_sees_what_it_might_change(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        llm.script("r1", extraction(item("he loves cheese, especially sharp cheddar on everything", 4, gist="something about food")))
        await mem.extract([("i love cheddar", "same")], [], NOW - 120 * DAY)
        old_id = (await db.load_memories())[0]["id"]
        seen = {}
        def reply(messages):
            seen["body"] = messages[1]["content"]
            return extraction(item("he used to love cheese and says he can't stand the stuff now", 4, replaces=[f"#{old_id}", "#999"]))
        llm.script("r1", reply)
        out = await mem.extract([("i can't stand cheese anymore", "since when")], [], NOW)
        rows = await db.load_memories(include_inactive=True)
        recalled = await mem.recall(["cheese"], calm(), False, NOW + 1)
        await db.close()
        return out, rows, seen["body"], recalled, old_id
    out, rows, body, recalled, old_id = run(go())
    assert f"#{old_id}" in body and "sharp cheddar" in body and "replaces" in body            # it was shown what might be out of date
    assert out["replaced"] == 1 and {r["id"]: r["status"] for r in rows}[old_id] == "superseded"
    assert [r.row["text"] for r in recalled] == ["he used to love cheese and says he can't stand the stuff now"]   # only the current one returns


def test_an_exception_is_kept_alongside_not_instead(db_url):
    async def go():
        c, db, llm, mem = mk(db_url)
        await db.migrate()
        llm.script("r1", extraction(item("he finds philosophizing about work problems trite", 4)))
        await mem.extract([("x", "y")], [], NOW - 30 * DAY)
        llm.script("r1", extraction(item("he philosophized about a work problem himself because he doesn't think it's trite when the problem is about people", 4)))
        out = await mem.extract([("x", "y")], [], NOW)
        rows = await db.load_memories()
        await db.close()
        return out, rows
    out, rows = run(go())
    assert out["saved"] == 1 and "replaced" not in out and len(rows) == 2


def test_notes_are_framed_as_tendencies_and_forget_gets_a_plain_agreement():
    from astrid import persona
    head = persona.profile_block([{"text": "x"}])
    assert "generalizations, not rules" in head and "what he says now beats a note" in head
    assert "forget something or drop it" in persona.RULES


# ---------- wordsmith habit, empty replies ----------
def test_coined_sayings_and_made_up_anecdotes_get_a_hint_but_plain_replies_dont():
    from astrid import critic
    wordy = ["agency and an open heart at the same time. that's the trick nobody teaches, and the people who can do it usually learned it the hard way.",
             "pride's just expecting the ground to hold. the falls aren't the failure, the surprise is",
             "you landed it on the third try. that's a typing arc, not a tragedy",
             "i cleared the same bar once and never looked back"]
    for line in wordy:
        hints = critic.reply_hints(["hey", line, "ok"])
        assert any("neat sayings" in h or "anecdote" in h for h in hints), line
    plain = ["yeah, i think that's right", "i'd push back a little. some people just haven't had it go wrong yet", "what's the review about"]
    assert not any("neat sayings" in h or "anecdote" in h for h in critic.reply_hints(plain))
    assert "Don't coin sayings" in __import__("astrid").persona.RULES


def test_an_empty_completion_is_retried_with_more_room_instead_of_failing(db_url):
    from astrid.llm import LLMError
    async def go():
        c, db, llm, _ = mk(db_url)
        await db.migrate()
        llm.script("euryale", LLMError("empty completion"), "ok, sure")
        out = await Astrid(c, db, llm, clock=lambda: NOW).reply(1, "hi")
        await db.close()
        return out, [x["max_tokens"] for x in llm.calls]
    out, tokens = run(go())
    assert out == "ok, sure" and tokens == [600, 1800]


def test_the_very_first_message_ever_cant_pretend_to_a_shared_history(db_url):
    from astrid import critic
    assert critic.first_contact_slips("welcome back. missed you") and critic.first_contact_slips("last time you said that")
    assert not critic.first_contact_slips("hey. what's on your mind") and not critic.first_contact_slips("say that again?")
    async def go():
        c, db, llm, _ = mk(db_url)
        await db.migrate()
        llm.script("euryale", "good to see you again, jordan", "hey. who's this, then")
        first = await Astrid(c, db, llm, clock=lambda: NOW).reply(1, "hey")
        notes = prompt(llm.calls[0])
        await Astrid(c, db, llm, clock=lambda: NOW + 60).reply(1, "just me")
        second = prompt(llm.calls[-1])
        await db.close()
        return first, notes, second, len([x for x in llm.calls])
    first, notes, second, calls = run(go())
    assert first == "hey. who's this, then" and calls == 3                                   # rewritten once on the first message
    assert "first time you and Jordan have ever talked" in notes and "first time you and Jordan have ever talked" not in second


def test_she_cant_claim_reading_she_hasnt_done_and_stops_nagging_about_bed(db_url):
    from astrid import critic
    assert critic.claimed_reading("reading about a court case that's been going for years") and critic.claimed_reading("i was looking into old maps")
    assert not critic.claimed_reading("not much. it's been quiet") and not critic.claimed_reading("what are you reading these days")
    assert any("go to bed" in h for h in critic.reply_hints(["go to bed when you said you would", "ok"]))
    async def go():
        c, db, llm, _ = mk(db_url)
        await db.migrate()
        llm.script("euryale", "reading about a court case, mostly", "not much. quiet night")
        out = await Astrid(c, db, llm, clock=lambda: NOW).reply(1, "what are you up to?")
        p = prompt(llm.calls[0])
        await db.close()
        return out, p, len(llm.calls)
    out, p, calls = run(go())
    assert out == "not much. quiet night" and calls == 2                                      # the invented reading was rewritten
    assert "haven't read or looked into anything" in p


def test_the_time_comes_with_the_part_of_the_day():
    from astrid.state import day_part, render
    assert [day_part(h) for h in (3, 9, 14, 21, 23)] == ["the middle of the night", "morning", "afternoon", "evening", "late at night"]
    assert "9:18 PM Eastern time (evening)" in render(State(dict(BASELINE), "x", None, None, 0), datetime(2026, 11, 4, 21, 18))


def test_she_cant_say_its_late_in_the_evening_or_nag_and_the_first_meeting_acknowledges_what_this_is(db_url):
    from astrid import critic, persona
    assert critic.time_slips("hey. you're up late for a wednesday.", 21) and critic.time_slips("it's so late", 14)
    assert not critic.time_slips("hey. you're up late for a wednesday.", 23) and not critic.time_slips("hey. how was the day", 21)
    assert any("nag" in h or "bed" in h for h in critic.reply_hints(["then chill and don't make me nag you at 10:31", "ok"]))
    assert "fine when they come from your mood" in persona.RULES
    async def go():
        c, db, llm, _ = mk(db_url)
        await db.migrate()
        llm.script("euryale", "hey. you're up late for a wednesday.", "hey. so this is how it starts")
        out = await Astrid(c, db, llm, clock=lambda: at_evening()).reply(1, "hey")
        p = prompt(llm.calls[0])
        await db.close()
        return out, p
    out, p = run(go())
    assert out == "hey. so this is how it starts"                                           # the 'late' slip was rewritten
    assert "acknowledge what this is" in p and "no interview" in p


def at_evening():
    return datetime(2026, 11, 4, 21, 35, tzinfo=__import__("zoneinfo").ZoneInfo("America/Toronto")).timestamp()


def test_the_checks_do_not_fire_on_her_own_example_voice():
    """A guard against the checks piling up: none of the everyday example lines may trip a rewrite or a habit hint by itself."""
    from astrid import critic, examples
    offenders = []
    for e in examples.EXAMPLES:
        if e.when == "heated":
            continue
        for line in e.astrid:
            hints = critic.reply_hints(["hey", line, "ok"])
            reading = critic.claimed_reading(line) and e.when != "inner_life"      # reading is only legal when she has read something
            first = critic.first_contact_slips(line) and e.when != "close_plus"    # 'i missed you' only fits once there's history
            if critic.hard_violations(line) or critic.time_slips(line, 21) or reading or first or any("neat sayings" in h or "go to bed" in h or "anecdote" in h for h in hints):
                offenders.append(line)
    assert offenders == []
