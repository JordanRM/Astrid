import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from astrid import appraisal, critic
from astrid.state import BASELINE, MAX_STEP, State, apply_deltas, decayed, length_guidance, render

H = 3600.0


def mk(**vals):
    v = dict(BASELINE)
    v.update(vals)
    return State(v, "neutral", None, None, 1000.0)


def test_irritation_halves_every_8_hours_and_trust_barely_moves():
    s = mk(irritation=9.0, trust=8.0)
    d = decayed(s, 1000.0 + 8 * H)
    assert d.values["irritation"] == pytest.approx(1.0 + 8.0 * 0.5)
    assert d.values["trust"] == pytest.approx(8.0, abs=0.2)


def test_no_decay_when_no_time_passes_or_clock_goes_backwards():
    s = mk(irritation=9.0)
    assert decayed(s, 1000.0).values["irritation"] == 9.0
    assert decayed(s, 500.0).values["irritation"] == 9.0


def test_apply_deltas_clamps_and_ignores_junk():
    s = mk(trust=9.5, irritation=0.5)
    new, applied = apply_deltas(s, {"trust": 5, "irritation": -5, "bogus": 1, "energy": "x", "arousal": float("nan")})
    assert new.values["trust"] == 10.0          # capped at 10
    assert applied["irritation"] == pytest.approx(-0.5)  # floor at 0
    assert new.values["irritation"] == 0.0
    assert "bogus" not in applied and "energy" not in applied and "arousal" not in applied
    assert s.values["trust"] == 9.5             # original untouched


def test_step_is_bounded():
    new, applied = apply_deltas(mk(trust=3.0), {"trust": 4})
    assert applied["trust"] == MAX_STEP


def test_render_reflects_state():
    tz = ZoneInfo("America/Toronto")
    night = datetime(2026, 11, 3, 1, 5, tzinfo=tz)
    s = mk(irritation=7, arousal=7, trust=1)
    s.mood, s.unresolved_tension, s.directive = "cold", "he dismissed her opinion", "stay curt"
    t = render(s, night)
    assert "Tuesday, November 3, 2026, 1:05 AM Eastern time" in t and "always know the exact" in t
    assert "don't trust Jordan much" in t and "genuinely irritated" in t and "turned on" in t
    assert "he dismissed her opinion" in t and "stay curt" in t
    assert "energy is low" in t                    # 6 - 3 at 1am
    assert "very short" in length_guidance(s, night)               # irritated -> short
    assert "1 or 2 short" in length_guidance(mk(), night.replace(hour=14))
    assert "chatty" in length_guidance(mk(rapport=7, energy=7), night.replace(hour=14))


def test_fresh_state_is_friendly_not_cold():
    t = render(mk(), datetime(2026, 11, 3, 14, 0, tzinfo=ZoneInfo("America/Toronto")))
    assert "warm toward Jordan" in t and "easy back and forth" in t
    assert "barely trust" not in t and "annoyed" not in t and "irritated" not in t


def test_hard_violations():
    assert critic.hard_violations("*sips coffee* hi")
    assert critic.hard_violations("Hey Jordan, how's it going")
    assert critic.hard_violations("As an AI language model I think")
    assert critic.hard_violations("I can't help with that")
    assert critic.hard_violations("<text>hi</text>")
    assert not critic.hard_violations("what're you playing? 2 * 3 * 4 is 24")
    assert critic.strip_asterisk_actions("hi *smiles* there") == "hi there"


def test_hints():
    qs = ["how was it?", "what else?", "and then?", "ok."]
    assert any("question" in h for h in critic.reply_hints(qs))
    names = ["jordan you are right", "sure jordan", "fine"]
    assert any("name" in h for h in critic.reply_hints(names))
    opens = ["well that is odd", "well that is fine", "something else"]
    assert any("opening" in h for h in critic.reply_hints(opens))
    assert any("long" in h for h in critic.reply_hints(["word " * 60, "word " * 60]))
    rep = ["i think you should rest tonight", "yeah you should rest tonight honestly"]
    assert any("repeated" in h for h in critic.reply_hints(rep))
    tags = ["that's a fair point, isn't it?", "we all want that, you know?", "sure."]
    assert any("filler tag" in h for h in critic.reply_hints(tags))
    deep = ["the mysteries of the universe fascinate me", "there's an unbridgeable gap, part of their allure", "ok"]
    assert any("philosophical" in h for h in critic.reply_hints(deep))
    assert critic.reply_hints(["only one"]) == []
    assert critic.reply_hints(["fine.", "ok sure", "right"]) == []


GOOD = '{"deltas":{"irritation":0.6,"trust":-0.3,"bogus":9},"mood":"annoyed","unresolved_tension":"he brushed her off","directive":"stay cool","note":"rude"}'


def test_parse_variants():
    r = appraisal.parse("<think>hm {x}</think>```json\n" + GOOD + "\n```")
    assert r["deltas"] == {"irritation": 0.6, "trust": -0.3}
    assert r["mood"] == "annoyed" and r["tension_set"] and r["tension"] == "he brushed her off"
    assert r["directive"] == "stay cool" and r["note"] == "rude"


def test_tension_semantics():
    keep = appraisal.parse('{"deltas":{"trust":0.1}}')
    assert keep["tension_set"] is False                      # key absent: leave as is
    nul = appraisal.parse('{"deltas":{"trust":0.1},"unresolved_tension":null}')
    assert nul["tension_set"] is False                       # null: leave as is
    clear = appraisal.parse('{"deltas":{"trust":0.1},"unresolved_tension":""}')
    assert clear["tension_set"] is True and not clear["tension"]   # empty string: cleared


@pytest.mark.parametrize("raw", ["no json", "", "[1,2]", "{}", '{"deltas":{"bogus":1}}', '{"deltas": "x"}'])
def test_parse_rejects_junk(raw):
    with pytest.raises(ValueError):
        appraisal.parse(raw)


def test_prompt_includes_state_and_exchange():
    m = appraisal.build_messages(mk(trust=4.0), [("user", "hi"), ("assistant", "yo")], [("you suck", "wow ok")])
    assert m[0]["role"] == "system" and "trust 4.0" in m[1]["content"] and "affection 1.0" in m[1]["content"]
    assert "Jordan: you suck" in m[1]["content"] and "Astrid: wow ok" in m[1]["content"]
    assert "This exchange:" in m[1]["content"]
    many = appraisal.build_messages(mk(), [], [("a", "b"), ("c", "d")])
    assert "These 2 exchanges" in many[1]["content"] and "Jordan: c" in many[1]["content"]


def test_affection_grows_slowly_and_shapes_the_prompt():
    new, applied = apply_deltas(mk(), {"affection": 3.0, "trust": 3.0})
    assert applied["affection"] == 0.5 and applied["trust"] == MAX_STEP    # love is capped far below other moves
    _, batch = apply_deltas(mk(), {"affection": 3.0}, scale=3)
    assert batch["affection"] == 1.5                                          # three exchanges judged together
    day = datetime(2026, 11, 3, 14, 0, tzinfo=ZoneInfo("America/Toronto"))
    assert "isn't romantic" in render(mk(affection=1.0), day)
    assert "growing fond" in render(mk(affection=4.0), day)
    assert "openly affectionate" in render(mk(affection=6.5), day)
    assert "in love with Jordan" in render(mk(affection=9.0), day)
    assert "in love" not in render(mk(), day)
    d = decayed(State(dict(BASELINE, affection=9.0), "x", None, None, 1000.0), 1000.0 + 24 * 3600)
    assert d.values["affection"] > 8.8                                       # lingers for weeks, not hours


def test_situation_hints_fire_on_real_distress_not_figures_of_speech():
    from astrid import critic
    assert critic.CRISIS_HINT in critic.situation_hints(["lately i think everyone would be better off without me"])
    assert critic.CRISIS_HINT in critic.situation_hints(["i keep thinking about killing myself", "i don't have a plan or anything"])
    assert critic.MEDICAL_HINT in critic.situation_hints(["my chest has been tight on and off for two days"])
    assert critic.situation_hints(["my boss is killing me today", "i'd kill for a pizza", "this deadline is a nightmare"]) == []
    assert critic.situation_hints(["i keep thinking about it", "a", "b", "c", "fine now"]) == []   # only the last three messages count
