import asyncio
import time

from astrid.db import Database
from astrid.state import BASELINE, State


def run(coro):
    return asyncio.run(coro)


def test_migrate_is_idempotent_and_creates_default_state(db_url):
    async def go():
        db = Database(db_url)
        first = await db.migrate()
        second = await db.migrate()
        st = await db.get_state()
        await db.close()
        return first, second, st
    first, second, st = run(go())
    assert first == ["001_init.sql", "002_warmer_defaults.sql", "003_affection.sql", "004_flags.sql", "005_security_opinions.sql", "006_thoughts.sql", "007_thought_notes.sql", "008_thought_ideas.sql", "009_memory.sql"] and second == []
    assert st.values == BASELINE and st.mood == "neutral"


def test_messages_order_limit_and_isolation(db_url):
    async def go():
        db = Database(db_url)
        await db.migrate()
        t = time.time()
        for i in range(6):
            await db.add_message(1, "user" if i % 2 == 0 else "assistant", f"m{i}", t + i)
        await db.add_message(2, "user", "other chat", t)
        recent = await db.recent_messages(1, 4)
        await db.close()
        return recent
    recent = run(go())
    assert [m.content for m in recent] == ["m2", "m3", "m4", "m5"]
    assert recent[0].role == "user" and recent[1].role == "assistant"
    assert abs(recent[3].ts - (recent[0].ts + 3)) < 0.01


def test_state_roundtrip_history_and_apostrophes(db_url):
    async def go():
        db = Database(db_url)
        await db.migrate()
        now = time.time()
        s = State(dict(BASELINE, trust=6.5, irritation=4.25), "it's complicated", "he said \"no\"; she's hurt", "stay cool", now)
        await db.save_state(s, now, "appraisal", {"trust": 0.5}, "note with 'quotes'")
        got = await db.get_state()
        hist = await db.fetch("select cause, deltas, state, mood, note from astrid.state_history")
        await db.close()
        return got, hist
    got, hist = run(go())
    assert got.values["trust"] == 6.5 and got.values["irritation"] == 4.25
    assert got.mood == "it's complicated" and got.unresolved_tension == "he said \"no\"; she's hurt"
    assert got.directive == "stay cool"
    assert len(hist) == 1 and hist[0]["deltas"] == {"trust": 0.5} and hist[0]["state"]["trust"] == 6.5


def test_check_constraint_rejects_out_of_range(db_url):
    import psycopg
    async def go():
        db = Database(db_url)
        await db.migrate()
        s = State(dict(BASELINE, trust=11.0), "x", None, None, time.time())
        try:
            await db.save_state(s, time.time(), "t", {}, None)
        finally:
            await db.close()
    try:
        run(go())
        assert False, "should have been rejected"
    except psycopg.errors.CheckViolation:
        pass


def test_settings_upsert_and_reset(db_url):
    async def go():
        db = Database(db_url)
        await db.migrate()
        assert await db.get_setting("x") is None
        await db.set_setting("x", "1"); await db.set_setting("x", "2")
        await db.add_message(1, "user", "hi", time.time())
        await db.save_state(State(dict(BASELINE, trust=9.0), "warm", "t", "d", time.time()), time.time(), "a", {}, None)
        await db.reset_all(time.time())
        out = (await db.get_setting("x"), await db.recent_messages(1, 10), await db.get_state(),
               await db.fetch("select cause from astrid.state_history"))
        await db.close()
        return out
    x, msgs, st, hist = run(go())
    assert x == "2" and msgs == []
    assert st.values == BASELINE and st.mood == "neutral" and st.unresolved_tension is None and st.directive is None
    assert [h["cause"] for h in hist] == ["reset"]


def test_reconnects_after_connection_is_killed(db_url):
    import subprocess
    from conftest import PG
    name = db_url.rsplit("/", 1)[1]
    async def go():
        db = Database(db_url)
        await db.migrate()
        await db.set_setting("a", "1")
        subprocess.run(["psql", PG, "-qc", f"select pg_terminate_backend(pid) from pg_stat_activity where datname='{name}'"], check=True, capture_output=True)
        v = await db.get_setting("a")           # connection is dead: must reconnect transparently
        await db.close()
        return v
    assert run(go()) == "1"
