import asyncio
import json
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import FakeLLM, make_cfg, prompt
from astrid.db import Database
from astrid.embed import HashEmbedder
from astrid.engine import Astrid
from astrid.memory import Memory
from astrid.outreach import Outreach, Plan, Tide, desire, quiet_window, workday
from astrid.state import BASELINE, State

TZ = ZoneInfo("America/Toronto")
H = 3600.0


def at(hour, minute=0, day=4):
    """November 2026 in Toronto. Day 4 is a Wednesday; 6 and 7 are Friday and Saturday."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp()


def run(coro):
    return asyncio.run(coro)


class Lucky:
    """A roll that always comes up, so a test can decide what she'd do without chasing probabilities."""
    def random(self): return 0.0
    def uniform(self, a, b): return (a + b) / 2
    def choices(self, pop, weights=None, k=1): return [max(zip(pop, weights or [1] * len(pop)), key=lambda t: t[1])[0]]


class Env:
    def __init__(self, db_url, now, **state):
        self.db_url, self.now, self.state = db_url, now, state
        self.sent = []

    async def start(self, user_ago=2 * H, level=0.8, cap=4, memory=True, lucky=True, **cfg):
        c = make_cfg(self.db_url, **cfg)
        self.cfg, self.db, self.llm = c, Database(self.db_url), FakeLLM()
        await self.db.migrate()
        vals = dict(BASELINE)
        vals.update(self.state)
        await self.db.save_state(State(vals, "warm", None, None, self.now), self.now, "t", {}, None)
        if user_ago is not None:
            await self.db.add_message(1, "user", "hey, long day", self.now - user_ago)
            await self.db.add_message(1, "assistant", "same. you ok", self.now - user_ago + 20)
        self.mem = Memory(c, self.db, self.llm, HashEmbedder(), clock=lambda: self.now) if memory else None
        self.engine = Astrid(c, self.db, self.llm, clock=lambda: self.now, memory=self.mem)
        async def send(chat_id, text):
            self.sent.append(text)
        self.o = Outreach(c, self.db, self.engine, send, self.mem, clock=lambda: self.now)
        if lucky:
            self.o.rng = Lucky()
        day = datetime.fromtimestamp(self.now, TZ).date().isoformat()
        await self.db.set_setting("outreach_tide", json.dumps({"level": level, "day": day, "cap": cap}))
        return self

    async def end_of_day_quiet(self):
        await self.db.close()


# ---------- hours ----------
def test_quiet_hours_are_roughly_10pm_to_730am_and_later_on_weekends():
    q = lambda h, m=0, d=4: quiet_window(datetime(2026, 11, d, h, m, tzinfo=TZ))
    assert q(23) and q(3) and q(7, 0) and not q(7, 45) and not q(12) and not q(21, 59) and q(22, 0)
    assert not q(22, 30, 6) and q(23, 30, 6)           # Friday night runs later
    assert q(8, 30, 7) and not q(9, 10, 7)             # Saturday morning runs later
    assert q(8, 30, 8) and not q(9, 10, 8)             # so does Sunday
    assert workday(datetime(2026, 11, 4, 14, tzinfo=TZ)) and not workday(datetime(2026, 11, 7, 14, tzinfo=TZ))


# ---------- ebb and flow ----------
def test_the_tide_comes_in_runs_not_a_steady_rate():
    from datetime import timedelta
    days = [(datetime(2026, 11, 1, 12) + timedelta(days=i)).date() for i in range(40)]
    busy, quiet = Tide(random.Random(1)), Tide(random.Random(1))
    caps_busy, caps_quiet = [], []
    for d in days:
        busy.for_day(d, 0.9); caps_busy.append(busy.cap())
        quiet.for_day(d, 0.0); caps_quiet.append(quiet.cap())
    assert sum(caps_busy) / 40 > 3 and max(caps_busy) <= 6
    assert 0 in caps_quiet and sum(caps_quiet) / 40 < 1.5                      # some days nothing at all
    assert len(set(caps_busy)) > 1                                              # and it isn't a flat number
    t = Tide(random.Random(3))
    t.for_day(days[0], 0.5); first = (t.level, t.cap())
    t.for_day(days[0], 0.5)
    assert (t.level, t.cap()) == first                                          # settled for the day, doesn't wobble
    # a quiet stretch carries over: after busy days, one slow day doesn't zero her out
    t2 = Tide(random.Random(5), level=0.9)
    t2.for_day(days[0], 0.0)
    assert t2.level > 0.3


# ---------- what she might want ----------
def test_a_loose_end_is_followed_up_once_after_it_has_had_time(db_url):
    async def go():
        e = await Env(db_url, at(18)).start()
        await e.mem.load()
        mid = await e.mem._store("loose_end", "he'll tell me how the project review on Friday goes", None, 4, "curious", 0.1, False, e.now - 20 * H)
        plans = {p.reason: p for p in await e.o.urges(e.now, State(dict(BASELINE), "x", None, None, e.now), datetime.fromtimestamp(e.now, TZ), e.now - 2 * H)}
        early_row = next(r for r in e.mem._rows if r["id"] == mid)
        early_row["created_at"] = e.now - 3 * H
        too_soon = [p.reason for p in await e.o.urges(e.now, State(dict(BASELINE), "x", None, None, e.now), datetime.fromtimestamp(e.now, TZ), e.now - 2 * H)]
        early_row["created_at"] = e.now - 20 * H
        e.llm.script("euryale", "how did the review go")
        text = await e.o.reach_out(plans["follow_up"])
        again = [p.reason for p in await e.o.urges(e.now, State(dict(BASELINE), "x", None, None, e.now), datetime.fromtimestamp(e.now, TZ), e.now - 2 * H)]
        row = [r for r in await e.db.load_memories() if r["id"] == mid][0]
        await e.db.close()
        return plans, too_soon, text, again, row
    plans, too_soon, text, again, row = run(go())
    assert "follow_up" in plans and "Friday" in plans["follow_up"].note and "follow_up" not in too_soon
    assert text == "how did the review go" and "follow_up" not in again and row["followed_up"] is True


def test_sharing_needs_a_real_reason_and_never_comes_from_explicit_reading(db_url):
    async def go():
        e = await Env(db_url, at(18), rapport=7).start(memory=False)
        base = {"kind": "thread", "question": "why do old maps show california as an island", "query": "q", "sources": [], "thought": "one bad map got copied for a century", "share": 0.8}
        await e.db.add_thought({**base, "for_jordan": None}, e.now - H)
        none = [p.reason for p in await e.o.urges(e.now, State(dict(BASELINE), "x", None, None, e.now), datetime.fromtimestamp(e.now, TZ), e.now - 2 * H)]
        await e.db.add_thought({**base, "for_jordan": "he said he likes how stubborn mistakes stick", "question": "other"}, e.now - H)
        await e.db.add_thought({**base, "for_jordan": "x", "explicit": True, "question": "hot"}, e.now - H)
        st = State({**BASELINE, "rapport": 7}, "x", None, None, e.now)
        plans = await e.o.urges(e.now, st, datetime.fromtimestamp(e.now, TZ), e.now - 2 * H)
        await e.db.close()
        return none, plans
    none, plans = run(go())
    assert "share" not in none
    share = [p for p in plans if p.reason == "share"]
    assert len(share) == 1 and "stubborn mistakes" in share[0].note and "hot" not in share[0].note and not share[0].heat


def test_missing_him_grows_with_silence_and_closeness_and_wanting_needs_arousal(db_url):
    async def go():
        e = await Env(db_url, at(20)).start(memory=False)
        loc = datetime.fromtimestamp(e.now, TZ)
        def st(**v): return State({**BASELINE, **v}, "x", None, None, e.now)
        async def urge(reason, state, silence):
            return next((p.urge for p in await e.o.urges(e.now, state, loc, e.now - silence) if p.reason == reason), 0.0)
        out = {
            "recent": await urge("miss", st(affection=7), 2 * H),
            "day": await urge("miss", st(affection=7), 12 * H),
            "days": await urge("miss", st(affection=7), 30 * H),
            "distant": await urge("miss", st(affection=2), 12 * H),
            "insecure": await urge("miss", st(affection=7, security=2), 12 * H),
            "calm": await urge("want", st(arousal=1), 2 * H),
            "turned_on": await urge("want", st(arousal=7), 2 * H),
            "no_affection_but_aroused": await urge("want", st(arousal=7, affection=1), 2 * H),
            "annoyed": await urge("want", st(arousal=7, irritation=6), 2 * H),
        }
        await e.db.close()
        return out
    u = run(go())
    assert u["recent"] == 0 < u["day"] < u["days"] and u["distant"] < u["day"] < u["insecure"]
    assert u["calm"] == 0 and u["turned_on"] > 0.3 and u["no_affection_but_aroused"] > 0.3 and u["annoyed"] == 0


def test_what_happened_between_them_lately_keeps_the_want_simmering(db_url):
    async def go():
        e = await Env(db_url, at(21)).start()
        await e.mem.load()
        loc = datetime.fromtimestamp(e.now, TZ)
        st = State({**BASELINE, "arousal": 2.5, "affection": 6}, "x", None, None, e.now)
        before = [p.reason for p in await e.o.urges(e.now, st, loc, e.now - 2 * H)]
        await e.mem._store("moment", "he told me what he'd do to me and i could hardly type", None, 7, "wanted", 0.9, True, e.now - 20 * H)
        after = [p.reason for p in await e.o.urges(e.now, st, loc, e.now - 2 * H)]
        await e.db.close()
        return before, after
    before, after = run(go())
    assert "want" not in before and "want" in after


# ---------- whether she does ----------
def test_gates_she_never_starts_before_he_has_written_and_never_cuts_into_a_conversation(db_url):
    async def go():
        e = await Env(db_url, at(18)).start(user_ago=None)
        loc = datetime.fromtimestamp(e.now, TZ)
        a = await e.o._gate(e.now, loc, None)
        await e.db.add_message(1, "user", "hi", e.now - 5 * 60)
        b = await e.o._gate(e.now, loc, e.now - 5 * 60)
        c = await e.o._gate(e.now + 2 * H, datetime.fromtimestamp(e.now + 2 * H, TZ), e.now - 5 * 60)
        await e.db.close()
        return a, b, c
    a, b, c = run(go())
    assert a == "he hasn't ever written" and b.startswith("mid-conversation") and c is None


def test_daily_cap_spacing_and_backing_off_when_he_doesnt_answer(db_url):
    async def go():
        e = await Env(db_url, at(18)).start(cap=2)
        loc = datetime.fromtimestamp(e.now, TZ)
        user_at = e.now - 2 * H
        async def put(rows): await e.db.set_setting("outreach_log", json.dumps(rows))
        await e.o._load_tide()
        out = {}
        await put([{"at": e.now - 30 * 60, "reason": "miss"}]); out["just_texted"] = await e.o._gate(e.now, loc, user_at - 20 * H)
        await put([{"at": e.now - 5 * H, "reason": "miss"}, {"at": e.now - 3 * H, "reason": "share"}]); out["cap"] = await e.o._gate(e.now, loc, e.now - 9 * H)
        await e.db.set_setting("outreach_tide", json.dumps({"level": 0.9, "day": loc.date().isoformat(), "cap": 6})); await e.o._load_tide()
        await put([{"at": e.now - 20 * H, "reason": "miss"}, {"at": e.now - 18 * H, "reason": "miss"}]); out["two_unanswered"] = await e.o._gate(e.now, loc, e.now - 30 * H)
        await put([{"at": e.now - 80 * H, "reason": "miss"}, {"at": e.now - 70 * H, "reason": "miss"}, {"at": e.now - 40 * H, "reason": "miss"}])
        out["three_unanswered"] = await e.o._gate(e.now, loc, e.now - 90 * H)
        await put([{"at": e.now - 40 * H, "reason": "miss"}, {"at": e.now - 38 * H, "reason": "miss"}]); out["two_but_long_ago"] = await e.o._gate(e.now, loc, e.now - 50 * H)
        await e.db.close()
        return out
    out = run(go())
    assert out["just_texted"] == "just texted" and out["cap"] == "enough for today"
    assert out["two_unanswered"] == "no answer to the last two, waiting" and out["three_unanswered"] == "no answer to the last few, leaving it"
    assert out["two_but_long_ago"] is None


def test_a_text_goes_out_is_logged_and_the_next_reply_isnt_confused(db_url):
    async def go():
        e = await Env(db_url, at(18), affection=7, rapport=7).start(user_ago=30 * H)
        e.llm.script("euryale", "it's been quiet. you ok")
        text = await e.o.tick()
        log = json.loads(await e.db.get_setting("outreach_log"))
        stored = (await e.db.recent_messages(1, 5))[-1]
        again = await e.o.tick()                                                 # right after: spacing holds
        await e.db.close()
        return text, e.sent, log, stored, again, prompt(e.llm.calls[0])
    text, sent, log, stored, again, p = run(go())
    assert text == "it's been quiet. you ok" and sent == [text] and stored.role == "assistant" and stored.content == text
    assert log[-1]["reason"] == "miss" and again is None
    assert "texting Jordan first" in p and "noticed" in p and "nothing that makes him owe you a reply" in p and "middle of the night" not in p


def test_at_night_it_is_rare_but_possible_and_a_normal_day_adds_up_to_a_few_texts(db_url):
    async def go():
        e = await Env(db_url, at(12), affection=7).start(memory=False, lucky=False)
        e.o.tide.level = 0.8
        quiet_miss = Plan("miss", 0.5, "")
        want = Plan("want", 0.8, "", heat=True)
        calm = State({**BASELINE, "arousal": 2}, "x", None, None, e.now)
        hot = State({**BASELINE, "arousal": 7}, "x", None, None, e.now)
        sat = lambda h, d=7: datetime(2026, 11, d, h, tzinfo=TZ)           # Saturday
        out = {
            "miss_evening": e.o.chances([quiet_miss], sat(19), calm)[0],
            "miss_3am": e.o.chances([quiet_miss], datetime(2026, 11, 4, 3, tzinfo=TZ), calm)[0],
            "want_evening": e.o.chances([want], sat(19), hot)[0],
            "want_3am": e.o.chances([want], datetime(2026, 11, 4, 3, tzinfo=TZ), hot)[0],
            "want_3am_not_really": e.o.chances([want], datetime(2026, 11, 4, 3, tzinfo=TZ), calm)[0],
            "miss_workday": e.o.chances([quiet_miss], datetime(2026, 11, 4, 14, tzinfo=TZ), calm)[0],
        }
        await e.db.close()
        return out
    c = run(go())
    assert 0 < c["miss_3am"] < c["miss_evening"] / 10                                       # possible, but rare
    assert 0 < c["want_3am"] < c["want_evening"] and c["want_3am_not_really"] < c["want_3am"] / 3
    assert c["miss_workday"] < c["miss_evening"]                                            # he's probably working
    per_day = c["miss_evening"] * 12 * 15                                                   # 15 waking hours of 5-minute checks
    assert 1 < per_day < 8                                                                  # an engaged day is a few texts, not dozens


def test_a_night_text_carries_a_note_for_the_next_reply_and_a_workday_want_plays_with_it(db_url):
    async def go():
        e = await Env(db_url, at(3, 10), arousal=7, affection=6, irritation=0).start(user_ago=9 * H)
        e.llm.script("euryale", "can't sleep and it's your fault. i keep thinking about your hands")
        loc = datetime.fromtimestamp(e.now, TZ)
        plans = await e.o.urges(e.now, State({**BASELINE, "arousal": 7, "affection": 6}, "x", None, None, e.now), loc, e.now - 9 * H)
        want = next(p for p in plans if p.reason == "want")
        text = await e.o.reach_out(want)
        asleep = prompt(e.llm.calls[0])
        arousal_after = (await e.db.get_state()).values["arousal"]
        e.now = at(8, 30)
        e.llm.script("euryale", "morning")
        await e.engine.reply(1, "ha. you're up early")
        morning = prompt(e.llm.calls[-1])
        await e.db.close()
        return text, asleep, arousal_after, morning, want
    text, asleep, arousal, morning, want = run(go())
    assert text.startswith("can't sleep") and want.heat and "middle of something heated" in asleep and "don't expect an answer" in asleep
    assert arousal >= 5.0                                                                  # she's in the mood now, so if he answers she's there
    assert "texted him first at 3:10 AM" in morning and "not ignoring you" in morning


def test_a_weekday_afternoon_want_can_play_with_him_being_busy(db_url):
    async def go():
        e = await Env(db_url, at(14), arousal=7, affection=3).start(user_ago=6 * H)
        loc = datetime.fromtimestamp(e.now, TZ)
        plans = await e.o.urges(e.now, State({**BASELINE, "arousal": 7, "affection": 3}, "x", None, None, e.now), loc, e.now - 6 * H)
        note = next(p for p in plans if p.reason == "want").note
        await e.db.close()
        return note
    assert "working hours" in run(go())


def test_a_draft_that_fails_the_checks_is_not_sent(db_url):
    async def go():
        e = await Env(db_url, at(18), affection=7).start(user_ago=30 * H, memory=False)
        e.llm.script("euryale", "*sighs* it's quiet", "*sighs softly*")
        text = await e.o.tick()
        await e.db.close()
        return text, e.sent
    assert run(go()) == (None, [])


def test_test_speed_makes_silence_and_waits_count_faster_and_real_time_is_unchanged(db_url):
    async def go():
        out = {}
        for speed in (1.0, 12.0):
            e = await Env(db_url, at(18)).start(outreach_speedup=speed)
            loc = datetime.fromtimestamp(e.now, TZ)
            st = State({**BASELINE, "affection": 7}, "x", None, None, e.now)
            out[("miss", speed)] = next((p.urge for p in await e.o.urges(e.now, st, loc, e.now - 3 * H) if p.reason == "miss"), 0.0)
            out[("chance", speed)] = e.o.chances([Plan("miss", 0.5, "x")], loc, st)[0]
            await e.db.set_setting("last_outreach", "{}")
            await e.db.add_message(1, "assistant", "hi", e.now - 3 * 60)     # three real minutes ago
            out[("gate", speed)] = await e.o._gate(e.now, loc, e.now - 3 * H)
            await e.db.close()
        return out
    out = run(go())
    assert out[("miss", 1.0)] == 0.0 and out[("miss", 12.0)] > 0                    # 3 real hours counts as 36
    assert out[("chance", 12.0)] > 10 * out[("chance", 1.0)]
    assert out[("gate", 1.0)] and out[("gate", 1.0)].startswith("mid-conversation")  # real time: still talking
    assert out[("gate", 12.0)] is None                                              # 3 minutes counts as 36: she may text


def test_a_close_bond_makes_a_text_likely_within_a_day_of_quiet_and_a_light_one_still_has_a_little(db_url):
    async def go():
        e = await Env(db_url, at(15)).start(memory=False)
        loc = datetime.fromtimestamp(e.now, TZ)               # a weekday afternoon, the dampened case
        out = {}
        for label, aff, sil in (("close", 8.8, 24), ("light", 2, 24), ("close_early", 8.8, 5)):
            st = State({**BASELINE, "affection": aff}, "x", None, None, e.now)
            plans = await e.o.urges(e.now, st, loc, e.now - sil * H)
            out[label] = e.o.chances(plans, loc, st)[0] if plans else 0.0
        await e.db.close()
        return out
    c = run(go())
    per_hour = lambda p: 12 * p
    assert 0.08 < per_hour(c["close"]) < 0.25                 # roughly a one-in-ten shot every hour once a day has gone by
    assert 0 < c["light"] < c["close"] / 2 and c["close_early"] < c["close"] / 3


def test_a_thought_she_already_shared_in_a_text_is_not_retold_when_he_answers(db_url):
    async def go():
        e = await Env(db_url, at(15), rapport=7).start(memory=False)
        base = {"kind": "thread", "question": "does liking come back after it goes flat", "query": "q", "sources": [],
                "thought": "it does, just quieter", "share": 0.8, "for_jordan": "he is losing the wanting to run"}
        await e.db.add_thought(base, e.now - H)
        st = State({**BASELINE, "rapport": 7}, "x", None, None, e.now)
        plan = next(p for p in await e.o.urges(e.now, st, datetime.fromtimestamp(e.now, TZ), e.now - 2 * H) if p.reason == "share")
        e.llm.script("euryale", "read up on whether the liking comes back after things go flat. it does, just quieter")
        await e.o.reach_out(plan)
        e.now += 60
        await e.engine.reply(1, "liking comes back after it goes flat? how so")
        asked = prompt(e.llm.calls[-1])
        row = (await e.db.list_thoughts())[0]
        await e.db.close()
        return asked, row["surfaced"]
    asked, surfaced = run(go())
    assert "you already told him about this yourself" in asked and surfaced == 1


def test_a_thin_answer_or_a_busy_for_now_answer_changes_how_soon_she_tries_again(db_url):
    import json as _json
    async def go():
        out = {}
        async def case(name, replies, texted_ago, earlier=()):
            e = await Env(db_url, at(15)).start(user_ago=None, memory=False)
            await e.db.execute("truncate astrid.messages restart identity")
            loc = datetime.fromtimestamp(e.now, TZ)
            rows = [{"at": e.now - ago, "reason": "miss"} for ago in earlier] + [{"at": e.now - texted_ago, "reason": "miss"}]
            await e.db.set_setting("outreach_log", _json.dumps(rows))
            await e.db.add_message(1, "user", "hey", e.now - 40 * H)                       # he has written before
            for at_, text in [(r["at"] + 20, "thinking of you") for r in rows]:
                await e.db.add_message(1, "assistant", text, at_)
            for ago, text in replies:
                await e.db.add_message(1, "user", text, e.now - ago)
            last = e.now - replies[-1][0] if replies else e.now - 40 * H
            out[name] = await e.o._gate(e.now, loc, last)
            await e.db.close()
        await case("thin_soon", [(2 * H, "k")], 2.2 * H)
        await case("thin_later", [(5.5 * H, "k")], 6.5 * H)
        await case("busy_soon", [(2 * H, "busy rn, talk later")], 2.2 * H)
        await case("busy_later", [(5 * H, "busy rn, talk later")], 5.2 * H)
        await case("engaged", [(2 * H, "oh that's funny, my day was a mess, long story, work stuff and then the car")], 2.2 * H)
        return out
    r = run(go())
    assert r["thin_soon"] and "short" in r["thin_soon"]
    assert r["thin_later"] is None                                    # a few hours on she can try again
    assert r["busy_soon"] and "busy" in r["busy_soon"] and r["busy_later"] is None
    assert r["engaged"] is None


def test_repeated_thin_answers_stretch_the_wait_and_a_busy_one_gets_a_gentle_nudge_not_a_guilt_trip(db_url):
    import json as _json
    async def go():
        e = await Env(db_url, at(15)).start(user_ago=None, memory=False)
        loc = datetime.fromtimestamp(e.now, TZ)
        await e.db.add_message(1, "user", "hey", e.now - 80 * H)
        # two texts in a row that each got a one-word answer, the latest 8 hours ago
        rows = [{"at": e.now - 30 * H, "reason": "miss"}, {"at": e.now - 8 * H, "reason": "miss"}]
        await e.db.set_setting("outreach_log", _json.dumps(rows))
        for r in rows:
            await e.db.add_message(1, "assistant", "thinking of you", r["at"] + 20)
            await e.db.add_message(1, "user", "k", r["at"] + 300)
        twice = await e.o._gate(e.now, loc, e.now - 8 * H + 300)                      # 8h is plenty after one, not after two
        await e.db.close()
        e2 = await Env(db_url, at(15)).start(user_ago=None, memory=False)
        await e2.db.execute("truncate astrid.messages restart identity")
        await e2.db.add_message(1, "user", "hey", e2.now - 40 * H)
        await e2.db.set_setting("outreach_log", _json.dumps([{"at": e2.now - 6 * H, "reason": "miss"}]))
        await e2.db.add_message(1, "assistant", "thinking of you", e2.now - 6 * H + 20)
        await e2.db.add_message(1, "user", "busy today, talk later", e2.now - 6 * H + 300)
        e2.llm.script("euryale", "no rush. hope the day lets up")
        await e2.o.reach_out(Plan("miss", 1.0, "You miss him."))
        asked = prompt(e2.llm.calls[-1])
        await e2.db.close()
        return twice, asked
    twice, asked = run(go())
    assert twice and "2 in a row" in twice
    assert "said he was busy and would get back to you" in asked and "no guilt" in asked


def test_how_a_thin_or_missing_answer_lands_depends_on_how_she_already_is():
    from astrid.outreach import reaction
    steady = {**BASELINE, "affection": 8, "security": 8.5, "energy": 7, "irritation": 0.5}
    shaky = {**BASELINE, "affection": 8, "security": 3.5}
    edgy = {**BASELINE, "affection": 8, "security": 7, "irritation": 3}
    cool = {**BASELINE, "affection": 1.5, "security": 6}
    assert abs(reaction(steady, "thin", 1)["security"]) < abs(reaction(shaky, "thin", 1)["security"])      # shaky takes it harder
    assert "irritation" in reaction(edgy, "thin", 1) and reaction(edgy, "thin", 1)["irritation"] > reaction(edgy, "thin", 1)["security"] * -1
    assert reaction(shaky, "thin", 1)["security"] < 0 and reaction(shaky, "thin", 1).get("vulnerability", 0) > 0   # hurt, not anger
    assert abs(reaction(cool, "none", 1)["security"]) < abs(reaction(shaky, "none", 1)["security"])         # caring less, feeling less
    assert abs(reaction(shaky, "none", 1)["security"]) > abs(reaction(shaky, "thin", 1)["security"])        # silence stings more than a short answer
    assert abs(reaction(shaky, "thin", 3)["security"]) > abs(reaction(shaky, "thin", 1)["security"])       # and it builds


def test_a_thin_answer_or_none_at_all_marks_her_mood_once_and_the_model_does_the_reading(db_url):
    import json as _json
    async def go():
        e = await Env(db_url, at(15), affection=8, security=4).start(user_ago=None, memory=False)
        await e.db.execute("truncate astrid.messages restart identity")
        await e.db.add_message(1, "user", "hey", e.now - 40 * H)
        await e.db.set_setting("outreach_log", _json.dumps([{"at": e.now - 3 * H, "reason": "miss"}]))
        await e.db.add_message(1, "assistant", "thinking of you", e.now - 3 * H + 20)
        await e.db.add_message(1, "user", "sure, whatever works", e.now - 3 * H + 300)
        e.llm.script("euryale", "brief")                                   # the model reads it as a brief answer
        before = (await e.db.get_state()).values["security"]
        await e.o._react(e.now)
        mid = (await e.db.get_state()).values["security"]
        await e.o._react(e.now)                                            # again: it was already felt
        again = (await e.db.get_state()).values["security"]
        log = _json.loads(await e.db.get_setting("outreach_log"))
        # no answer at all, long enough to notice
        await e.db.execute("truncate astrid.messages restart identity")
        await e.db.add_message(1, "user", "hey", e.now - 40 * H)
        await e.db.set_setting("outreach_log", _json.dumps([{"at": e.now - 8 * H, "reason": "miss"}]))
        await e.o._react(e.now)
        ignored = (await e.db.get_state()).values["security"]
        await e.db.close()
        return before, mid, again, log, ignored
    before, mid, again, log, ignored = run(go())
    assert mid < before and again == mid and log[0]["how"] == "thin" and log[0]["felt"] == "thin"
    assert ignored < mid


def test_the_word_list_knows_cant_talk_and_ill_text_you_in_a_bit():
    from astrid.outreach import classify
    for t in ("can't talk right now", "i'll text you in a bit", "ill text you later", "slammed, talk soon", "in a meeting"):
        assert classify([t]) == "busy", t
    assert classify(["k"]) == "thin" and classify(["ha yeah that was a mess, tell me about yours, i've got time"]) == "engaged"
