import asyncio
import json

from astrid.db import Database
from astrid.engine import Astrid
from astrid.state import BASELINE
from conftest import FakeLLM, make_cfg

APPRAISAL = json.dumps({
    "deltas": {"irritation": 1.5, "trust": -0.5}, "mood": "annoyed",
    "unresolved_tension": "he called her useless", "directive": "stay curt, don't cave", "note": "insult",
})


def setup(db_url, clock):
    llm = FakeLLM()
    db = Database(db_url)
    return db, llm, Astrid(make_cfg(db_url), db, llm, clock=clock)


def test_reply_stores_both_sides_and_prompt_has_state(db_url):
    async def go():
        t = [1_800_000_000.0]
        db, llm, a = setup(db_url, lambda: t[0])
        await db.migrate()
        llm.script("euryale", "what're you playing?")
        out = await a.reply(7, "playing guitar")
        msgs = await db.recent_messages(7, 10)
        await db.close()
        return out, msgs, llm.calls
    out, msgs, calls = asyncio.run(go())
    assert out == "what're you playing?"
    assert [(m.role, m.content) for m in msgs] == [("user", "playing guitar"), ("assistant", "what're you playing?")]
    sysmsg = calls[0]["messages"][0]["content"]
    assert calls[0]["model"] == "euryale" and calls[0]["messages"][-2] == {"role": "user", "content": "playing guitar"}
    assert "You are Astrid" in sysmsg and "Eastern time" in sysmsg and "warm toward Jordan" in sysmsg
    last = calls[0]["messages"][-1]
    assert last["role"] == "system" and "Reply now as Astrid" in last["content"] and "Length:" in last["content"]
    assert calls[0]["extra"] == {"provider": {"sort": "latency"}}


def test_hard_violation_regenerates_once(db_url):
    async def go():
        db, llm, a = setup(db_url, lambda: 1_800_000_000.0)
        await db.migrate()
        llm.script("euryale", "*sips coffee* hey there", "hey")
        out = await a.reply(1, "hi")
        await db.close()
        return out, llm.calls
    out, calls = asyncio.run(go())
    assert out == "hey" and len(calls) == 2
    assert "broke the rules" in calls[1]["messages"][-1]["content"]


def test_second_violation_is_cleaned_not_looped(db_url):
    async def go():
        db, llm, a = setup(db_url, lambda: 1_800_000_000.0)
        await db.migrate()
        llm.script("euryale", "*grins* hi", "*grins* hello")
        out = await a.reply(1, "hi")
        await db.close()
        return out, len(llm.calls)
    out, n = asyncio.run(go())
    assert out == "hello" and n == 2


def test_critic_hints_and_gap_note_reach_the_prompt(db_url):
    async def go():
        t = [1_800_000_000.0]
        db, llm, a = setup(db_url, lambda: t[0])
        await db.migrate()
        for i in range(4):
            await db.add_message(1, "user", f"u{i}", t[0] - 20000 + i)
            await db.add_message(1, "assistant", f"how about that number {i}?", t[0] - 20000 + i + 0.5)
        llm.script("euryale", "fine")
        await a.reply(1, "back again")
        await db.close()
        return llm.calls[0]["messages"][0]["content"], llm.calls[0]["messages"][-1]["content"]
    sysmsg, reminder = asyncio.run(go())
    assert "ended with a question" in reminder                      # habit hints ride in the final reminder
    assert "previous message before this one was about 5 hours ago" in sysmsg


def test_appraisal_updates_state_and_next_reply_feels_it(db_url):
    async def go():
        t = [1_800_000_000.0]
        db, llm, a = setup(db_url, lambda: t[0])
        await db.migrate()
        llm.script("euryale", "wow ok", "still not talking about it")
        llm.script("r1", APPRAISAL)
        r = await a.reply(1, "you're useless")
        await a.appraise(1, "you're useless", r)
        st = await db.get_state()
        t[0] += 600                                   # ten minutes later
        await a.reply(1, "hello?")
        sysmsg = llm.calls[-1]["messages"][0]["content"]
        hist = await db.fetch("select cause, deltas from astrid.state_history")
        await db.close()
        return st, sysmsg, hist, [c["model"] for c in llm.calls]
    st, sysmsg, hist, models = asyncio.run(go())
    assert st.mood == "annoyed" and st.unresolved_tension == "he called her useless"
    assert st.values["irritation"] == BASELINE["irritation"] + 1.5 and st.values["trust"] == BASELINE["trust"] - 0.5
    assert "he called her useless" in sysmsg and "stay curt, don't cave" in sysmsg and "a little annoyed" in sysmsg
    assert models == ["euryale", "r1", "euryale"]
    assert hist[0]["cause"] == "appraisal"


def test_appraisal_failures_never_raise_and_leave_state_alone(db_url):
    async def go():
        db, llm, a = setup(db_url, lambda: 1_800_000_000.0)
        await db.migrate()
        await db.add_message(1, "user", "hi", 1_800_000_000.0)
        await db.add_message(1, "assistant", "yo", 1_800_000_001.0)
        for bad in (RuntimeError("boom"), "no json at all", '{"deltas": {}}'):
            llm.script("r1", bad)
            await a.appraise(1, "hi", "yo")
        st = await db.get_state()
        hist = await db.fetch("select 1 from astrid.state_history")
        await db.close()
        return st, hist
    st, hist = asyncio.run(go())
    assert st.values == BASELINE and st.mood == "neutral" and hist == []


def test_irritation_cools_over_the_day(db_url):
    async def go():
        t = [1_800_000_000.0]
        db, llm, a = setup(db_url, lambda: t[0])
        await db.migrate()
        llm.script("euryale", "wow ok", "hm")
        llm.script("r1", APPRAISAL)
        r = await a.reply(1, "you're useless")
        await a.appraise(1, "you're useless", r)
        t[0] += 16 * 3600                             # two half-lives later
        await a.reply(1, "morning")
        sysmsg = llm.calls[-1]["messages"][0]["content"]
        await db.close()
        return sysmsg
    sysmsg = asyncio.run(go())
    assert "annoyed" not in sysmsg.replace("Your mood right now: annoyed", "")   # irritation back below the cut-off
    assert "he called her useless" in sysmsg     # but the unresolved tension still lingers until repaired


def test_concurrent_messages_are_serialized(db_url):
    async def go():
        db, llm, a = setup(db_url, lambda: 1_800_000_000.0)
        await db.migrate()
        llm.script("euryale", lambda m: "r%d" % len(m))
        outs = await asyncio.gather(a.reply(1, "one"), a.reply(1, "two"))
        msgs = await db.recent_messages(1, 10)
        await db.close()
        return outs, [m.role for m in msgs]
    outs, roles = asyncio.run(go())
    assert roles == ["user", "assistant", "user", "assistant"]   # not interleaved
