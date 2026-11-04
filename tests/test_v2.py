import asyncio
import json
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from astrid import appraisal, examples, opinions, texting
from astrid.db import Database
from astrid.engine import Astrid
from astrid.state import BASELINE, State, decayed, render
from conftest import FakeLLM, make_cfg

TZ = ZoneInfo("America/Toronto")


def at(h, m=0):
    return datetime(2026, 11, 3, h, m, tzinfo=TZ)


def st(**v):
    return State(dict(BASELINE, **v), "x", None, None, 0.0)


# ---- texting ----
def test_normalize_lowercases_strips_dashes_prefix_and_slashes():
    assert texting.normalize("You: Hey. I'm Here — finally.  // And I'm Glad") == "hey. i'm here, finally.\n\nand i'm glad"


def test_split_texts_on_blank_lines_and_caps_at_three():
    assert texting.split_texts("one\n\ntwo") == ["one", "two"]
    assert texting.split_texts("a\n\nb\n\nc\n\nd\n\ne") == ["a", "b", "c d e"]
    assert texting.split_texts("single line\nstill one") == ["single line\nstill one"]


# ---- examples ----
def test_conditions_follow_state_and_time():
    c = examples.active_conditions(st(affection=6, arousal=5, irritation=1), at(23, 40))
    assert {"close", "close_plus", "heated", "late_night"} <= c
    c = examples.active_conditions(st(affection=6, arousal=5, irritation=3), at(14))
    assert "heated" not in c and "annoyed" in c
    assert {"low_security"} <= examples.active_conditions(st(security=2), at(14))
    assert "irritated" in examples.active_conditions(st(irritation=6), at(14))


def test_choose_is_deterministic_and_never_shows_heated_examples_to_a_stranger():
    heated = {e.jordan for e in examples.EXAMPLES if e.when == "heated"}
    for seed in range(40):
        picks = examples.choose(st(), at(15), random.Random(seed))
        assert len(picks) == 8 and not heated & {e.jordan for e in picks} and all(e.when != "inner_life" for e in picks)
    assert examples.choose(st(), at(15), random.Random(1)) == examples.choose(st(), at(15), random.Random(1))
    seen = set()
    for seed in range(40):
        seen |= {e.jordan for e in examples.choose(st(affection=6, arousal=6), at(23), random.Random(seed))}
    assert heated & seen


def test_render_shows_separate_texts_as_separate_lines():
    out = examples.render([examples.Example("any", "hi", ("a", "b"))])
    assert "Jordan: hi\nYou: a\nYou: b" in out


# ---- state ----
def test_security_decays_toward_baseline_and_shows_in_the_prompt():
    d = decayed(State(dict(BASELINE, security=1.0), "x", None, None, 1000.0), 1000.0 + 14 * 24 * 3600)
    assert abs(d.values["security"] - 3.5) < 0.01                               # half way back after the 14 day half-life
    assert "insecure" in render(st(security=2), at(15)) and "secure with Jordan" in render(st(security=8), at(15))
    assert "insecure" not in render(st(), at(15))


def test_she_only_initiates_when_close_calm_and_resolved():
    assert "bring it up yourself" in render(st(arousal=7, affection=6), at(23))
    assert "bring it up yourself" not in render(st(arousal=7, affection=2), at(23))
    assert "bring it up yourself" not in render(st(arousal=7, affection=6, irritation=3), at(23))


# ---- appraisal parsing ----
def test_parse_reads_and_cleans_opinions():
    raw = json.dumps({"deltas": {"security": -0.4, "trust": 9}, "mood": "warm", "opinions": [
        {"topic": "Best 70s album", "view": "rumours", "reason": "every song survived", "source": "READ", "confidence": 3, "note": ""},
        {"topic": "", "view": "no topic"}, "junk", {"topic": "t2", "view": "v2", "source": "made up", "confidence": "high"},
        {"topic": "t3", "view": "v3"}]})
    out = appraisal.parse(raw)
    assert out["deltas"]["security"] == -0.4
    assert [o["topic"] for o in out["opinions"]] == ["Best 70s album", "t2"]                    # junk dropped, capped at 2
    assert out["opinions"][0]["source"] == "read" and out["opinions"][0]["confidence"] == 1.0
    assert out["opinions"][1]["source"] == "conversation" and out["opinions"][1]["confidence"] == 0.5
    assert appraisal.parse(json.dumps({"opinions": [{"topic": "a", "view": "b"}]}))["opinions"]   # opinions alone are usable


def test_appraisal_prompt_includes_held_views():
    msgs = appraisal.build_messages(st(), [], [("hi", "hey")], [{"topic": "best album", "view": "hejira", "confidence": 0.7}])
    assert "best album: hejira" in msgs[1]["content"] and "security" in msgs[0]["content"]


# ---- opinions ----
def op(topic, view, **kw):
    return {"topic": topic, "view": view, "reason": "because", "source": "conversation", "confidence": 0.6, "updated_at": 1.0, **kw}


def test_relevance_matches_topic_words_and_ranks_best_first():
    ops = [op("best album of the 70s", "hejira"), op("truffle oil", "it's fake"), op("november", "underrated")]
    assert [o["topic"] for o in opinions.relevant(ops, ["what's the best album ever?"])] == ["best album of the 70s"]
    assert opinions.relevant(ops, ["totally unrelated chatter"]) == []
    assert opinions.render([]) is None and "Why: because" in opinions.render(ops[:1])


def test_upsert_forms_keeps_and_records_changes_of_mind(db_url):
    async def go():
        db = Database(db_url)
        await db.migrate()
        a = await db.upsert_opinion("Best Album  of the 70s", "hejira", "unguarded", "read", 0.7, None, 100.0)
        b = await db.upsert_opinion("best album of the 70s", "Hejira", "still unguarded", "conversation", 0.8, None, 200.0)
        c = await db.upsert_opinion("best album of the 70s", "blue", "rawer, per jordan", "conversation", 0.5, "his point about nothing to hide behind", 300.0)
        rows = await db.list_opinions()
        await db.reset_all(400.0)
        after = await db.list_opinions()
        await db.close()
        return a, b, c, rows, after
    a, b, c, rows, after = asyncio.run(go())
    assert (a, b, c) == ("formed", "kept", "changed") and len(rows) == 1 and after == []
    r = rows[0]
    assert r["topic"] == "best album of the 70s" and r["view"] == "blue" and r["confidence"] == 0.5
    assert [(h["view"], h["note"]) for h in r["history"]] == [("Hejira", "his point about nothing to hide behind")]


# ---- engine ----
def test_reply_is_lowercased_and_sees_examples_and_relevant_views(db_url):
    async def go():
        cfg, db, llm = make_cfg(db_url), Database(db_url), FakeLLM()
        await db.migrate()
        await db.upsert_opinion("best album of the 70s", "hejira", "unguarded writing", "conversation", 0.7, None, 1.0)
        llm.script("euryale", "Hejira. Still.")
        reply = await Astrid(cfg, db, llm, rng=random.Random(3)).reply(1, "best album of the 70s?")
        stored = (await db.recent_messages(1, 5))[-1].content
        await db.close()
        return reply, stored, llm.calls[0]["messages"][0]["content"]
    reply, stored, system = asyncio.run(go())
    assert reply == stored == "hejira. still."
    assert "Views you've already formed" in system and "best album of the 70s: hejira" in system and "Examples of your voice" in system


def test_appraisal_saves_opinions_and_a_changed_mind_is_recorded(db_url):
    def judged(view, conf, note=""):
        return json.dumps({"deltas": {"rapport": 0.3}, "mood": "warm", "opinions": [
            {"topic": "best album of the 70s", "view": view, "reason": "r", "source": "conversation", "confidence": conf, "note": note}]})

    async def go():
        cfg, db, llm = make_cfg(db_url), Database(db_url), FakeLLM()
        await db.migrate()
        llm.script("euryale", "hejira")
        eng = Astrid(cfg, db, llm)
        llm.script("r1", judged("hejira", 0.7))
        await eng.appraise(1, "best album?", "hejira")
        llm.script("r1", judged("blue", 0.5, "his point"))
        await eng.appraise(1, "blue is rawer", "ok, halfway")
        rows = await db.list_opinions()
        await db.close()
        return rows
    rows = asyncio.run(go())
    assert rows[0]["view"] == "blue" and rows[0]["history"][0]["view"] == "hejira"
