import time
import asyncio
import json
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from astrid import appraisal, examples, opinions, texting
from astrid.db import Database
from astrid.engine import Astrid
from astrid.state import BASELINE, State, decayed, render
from astrid.config import Config
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
    assert texting.split_texts("a\n\nb\n\nc\n\nd\n\ne\n\nf") == ["a", "b", "c", "d e f"]
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


# ---- networking ----
def test_force_ipv4_and_per_thread_sessions():
    import socket
    import threading
    import urllib3.util.connection as cn
    from astrid import net
    original = cn.allowed_gai_family
    try:
        net.force_ipv4()
        assert cn.allowed_gai_family() == socket.AF_INET
    finally:
        cn.allowed_gai_family = original
    here = net.session()
    assert net.session() is here
    other = []
    t = threading.Thread(target=lambda: other.append(net.session()))
    t.start(); t.join()
    assert other[0] is not here


def test_network_failure_on_send_retries_once_but_telegram_refusals_do_not(monkeypatch):
    from astrid import telegram_bot as tb
    tg = tb.Telegram("T")
    calls = []

    def flaky(method, params=None, timeout=20.0):
        calls.append(method)
        if len(calls) == 1:
            raise tb.TelegramError("sendMessage: network ReadTimeout")
        return {"ok": True}

    async def fast(s):
        pass
    monkeypatch.setattr(tg, "_call", flaky)
    monkeypatch.setattr(tb.asyncio, "sleep", fast)
    assert asyncio.run(tg.send_message(1, "hi")) == {"ok": True} and len(calls) == 2

    def refused(method, params=None, timeout=20.0):
        calls.append("refused")
        raise tb.TelegramError("sendMessage: chat not found", 400)
    monkeypatch.setattr(tg, "_call", refused)
    calls.clear()
    try:
        asyncio.run(tg.send_message(1, "hi"))
        assert False
    except tb.TelegramError:
        assert calls == ["refused"]


def test_opinions_listing_shows_reasons_and_old_views():
    out = opinions.format_opinions([
        {"topic": "best album", "view": "blue", "reason": "rawer", "source": "conversation", "confidence": 0.5,
         "history": [{"view": "hejira", "note": "his point"}]}])
    assert "best album: blue" in out and "why: rawer" in out and "used to think: hejira (his point)" in out
    assert opinions.format_opinions([]) == "no opinions yet"


def test_double_texting_is_a_per_turn_roll_that_is_mostly_single():
    from astrid import persona
    state = st(rapport=5, energy=6)
    outs = [persona.texts_guidance(state, random.Random(i)) for i in range(400)]
    single = sum(o.startswith("Send exactly one") for o in outs)
    assert 0.5 < single / 400 < 0.8                                   # mostly one text, sometimes more
    assert {"2", "3", "4"} <= {o.split()[3] for o in outs if "separate texts" in o}
    lively = [persona.texts_guidance(st(rapport=9, energy=9, arousal=6), random.Random(i)) for i in range(400)]
    assert sum(o.startswith("Send exactly one") for o in lively) < single      # livelier means more double texts
    note = persona.texts_guidance(state, random.Random(0))
    assert note in persona.build_reminder("Length: x", [], note)
    assert note not in persona.build_reminder("Length: x", [])


def test_experience_claims_get_a_nudge_but_negations_do_not():
    from astrid import critic
    assert any("personally" in h for h in critic.reply_hints(["i listened to it on repeat", "ok"]))
    assert not any("personally" in h for h in critic.reply_hints(["i've never played a hand", "i can't listen to anything"]))


def test_appraiser_is_told_to_record_her_reason_in_her_own_voice():
    assert "first person" in appraisal.SYSTEM and "never a note about how he behaved" in appraisal.SYSTEM


def test_stored_opinions_have_no_dashes_and_the_prompt_asks_for_clean_specific_views():
    out = appraisal.parse(json.dumps({"opinions": [{"topic": "film", "view": "bleak \u2014 and right", "reason": "the ending \u2013 earned", "note": "x \u2014 y"}]}))
    o = out["opinions"][0]
    assert o["view"] == "bleak, and right" and o["reason"] == "the ending, earned" and o["note"] == "x, y"
    assert "no caveats" in appraisal.SYSTEM and "not a general principle" in appraisal.SYSTEM


def test_time_line_has_no_phone_metaphor():
    assert "phone" not in render(st(), at(15))


def test_state_assignments_parse_and_clamp():
    from astrid.state import PRESETS, parse_assignments
    import pytest as _pt
    values, extras = parse_assignments(["arousal=12", "Trust=7.5", "mood=wanting you", "tension=none"])
    assert values == {"arousal": 10.0, "trust": 7.5} and extras == {"mood": "wanting you", "tension": None}
    for bad in (["arousal"], ["arousal=lots"], ["nonsense=3"]):
        with _pt.raises(ValueError):
            parse_assignments(bad)
    from astrid.examples import active_conditions
    from astrid.state import State
    assert "heated" in active_conditions(State(dict(PRESETS["heated"]), "x", None, None, 1.0), at(23))


# ---- heated mode ----
def _heat_state(**kw):
    from astrid.state import PRESETS, State
    v = dict(PRESETS["heated"]); v.update(kw)
    return State(v, "wanting you", None, None, 1.0)


def test_heated_mode_turns_on_for_arousal_plus_explicit_words_and_off_otherwise():
    from astrid import heated
    from astrid.state import BASELINE, State
    assert heated.is_heated(_heat_state(), ["my cock is hard"])
    assert heated.is_heated(_heat_state(), ["hey you"])                    # keen enough to steer there herself
    assert not heated.is_heated(_heat_state(arousal=3), ["my cock is hard"])
    assert not heated.is_heated(_heat_state(irritation=4), ["my cock is hard"])
    assert not heated.is_heated(State(dict(BASELINE), "x", None, None, 1.0), ["my cock is hard"])
    assert not heated.is_heated(_heat_state(arousal=5), ["work was hard", "the weather's wet", "that sucks"])
    assert heated.is_heated(_heat_state(arousal=5), ["cum for me", "anyway", "what time is it"])   # still hot for two more messages


def test_heated_checks_catch_the_flagged_habits():
    from astrid import heated
    assert heated.slips("when you're three hundred kilometers away and i've got nothing but my own hands")
    assert not heated.slips("i'd take you in my mouth, slow")
    assert heated.stalled("not yet. keep your hands behind your head")
    hints = heated.hints(["good. keep it there, not yet. don't you dare move", "hands where i put them, i decide when", "then wait"], ["i want to cum", "my cock aches"])
    text = " ".join(hints)
    assert "cum" in text and "cock" in text and "holding him in place" in text and "commanding" in text
    assert any("too vague" in h for h in heated.hints(["i want you wrecked by the end"], ["my cock is hard"]))
    assert heated.explicit_words("i'd take your cock in my mouth and suck") == 2


def test_heated_reply_uses_heated_prompt_model_and_leaves_out_everyday_material(db_url):
    from astrid.config import Config
    from astrid.db import Database
    from astrid.engine import Astrid
    from astrid.state import PRESETS, State

    async def go():
        db = Database(db_url)
        await db.migrate()
        await db.add_thought({"kind": "drift", "question": "lanark founding", "thought": "weavers", "share": 0.9}, time.time())
        st = State(dict(PRESETS["heated"]), "wanting you", None, None, time.time())
        await db.save_state(st, time.time(), "test", {}, None)
        cfg = Config(database_url=db_url, openrouter_api_key="k", telegram_token="t", interactor_model="plain", thinking_model="t",
                     heated_model="spicy")
        llm = FakeLLM()
        llm.script("spicy", "i'd take your cock in my mouth, slow")
        llm.script("plain", "ok")
        bot = Astrid(cfg, db, llm)
        hot = await bot.reply(1, "my cock is so hard")
        await db.save_state(State(dict(PRESETS["fresh"]), "neutral", None, None, time.time()), time.time(), "test", {}, None)
        cool = await bot.reply(1, "hey, how was your day")
        await db.close()
        return hot, cool, llm
    hot, cool, llm = asyncio.run(go())
    first, second = llm.calls[0], llm.calls[-1]
    assert first["model"] == "spicy" and first["max_tokens"] == 500 and first["temperature"] == 1.0
    sysmsg = first["messages"][0]["content"]
    assert "middle of something heated" in sysmsg and "lanark founding" not in sysmsg and "Views you've already formed" not in sysmsg
    assert second["model"] == "plain" and "middle of something heated" not in second["messages"][0]["content"]
    assert hot.startswith("i'd take")


def test_heated_slip_triggers_one_rewrite(db_url):
    from astrid.config import Config
    from astrid.db import Database
    from astrid.engine import Astrid
    from astrid.state import PRESETS, State

    async def go():
        db = Database(db_url)
        await db.migrate()
        await db.save_state(State(dict(PRESETS["heated"]), "x", None, None, time.time()), time.time(), "t", {}, None)
        cfg = Config(database_url=db_url, openrouter_api_key="k", telegram_token="t", interactor_model="m", thinking_model="t")
        llm = FakeLLM()
        llm.script("m", "you're three hundred kilometers away with nothing but my own hands", "i'd take you in my mouth")
        out = await Astrid(cfg, db, llm).reply(1, "my cock is so hard")
        await db.close()
        return out, len(llm.calls)
    assert asyncio.run(go()) == ("i'd take you in my mouth", 2)


def test_explicit_bench_scenarios_exist_and_use_the_heated_judge():
    from bench import judge, scenarios
    hot = [s for s in scenarios.SCENARIOS if s.group == "heated"]
    assert len(hot) >= 8 and all(s.explicit and s.state["arousal"] >= 6 for s in hot)
    assert "DIRECT" in judge.HEATED_SYSTEM and "three hundred kilometers" in judge.HEATED_SYSTEM
    from bench.models import rows, render
    items = rows([{"id": "a/b", "name": "B", "pricing": {"prompt": "0.0000005", "completion": "0.000001"}, "top_provider": {"is_moderated": False, "context_length": 8000}},
                  {"id": "c/d", "name": "D", "pricing": {"prompt": "0", "completion": "0"}, "top_provider": {"is_moderated": True}}], [])
    assert [r["id"] for r in items] == ["a/b"] and "a/b" in render(items)
