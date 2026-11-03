import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from astrid import appraisal, critic
from astrid.state import BASELINE, MAX_STEP, State, apply_deltas, decayed, render

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
    assert "Tuesday November 3, 1:05 AM" in t
    assert "barely trust" in t and "genuinely irritated" in t and "turned on" in t
    assert "he dismissed her opinion" in t and "stay curt" in t
    assert "very short" in t                       # irritated -> short
    assert "energy is low" in t                    # 6 + (-2.5) at 1am


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
    m = appraisal.build_messages(mk(trust=4.0), [("user", "hi"), ("assistant", "yo")], "you suck", "wow ok")
    assert m[0]["role"] == "system" and "trust 4.0" in m[1]["content"]
    assert "Jordan: you suck" in m[1]["content"] and "Astrid: wow ok" in m[1]["content"]
