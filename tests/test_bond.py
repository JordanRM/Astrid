import random

from astrid import bond
from astrid.state import BASELINE, apply_deltas, State


def day_of_warmth(chem, openness, exchanges=40):
    plan = {"chem": chem, "open": openness, "spent": {}}
    s = State(dict(BASELINE), "neutral", None, None, 0.0)
    for _ in range(exchanges):
        s, _ = apply_deltas(s, bond.gate({"trust": 0.5, "rapport": 0.6, "affection": 0.5}, plan))
    return s.values


def test_a_long_warm_chat_on_the_first_day_does_not_reach_love():
    v = day_of_warmth(1.0, 1.0)
    assert v["affection"] < 3.0 and v["trust"] < 6.0           # fond at most, not in love
    # the same chat with a guarded person stays cooler still, and a spark gets further but not all the way
    assert day_of_warmth(0.6, 0.6)["affection"] < v["affection"] < day_of_warmth(2.0, 1.5)["affection"] < 6.0


def test_gains_past_the_allowance_still_count_a_little_and_losses_are_never_limited():
    plan = {"chem": 1.0, "open": 1.0, "spent": {"affection": 1.0}}
    assert 0 < bond.gate({"affection": 0.5}, plan)["affection"] < 0.1
    assert bond.gate({"affection": -0.5, "irritation": 1}, plan) == {"affection": -0.5, "irritation": 1}


def test_openness_comes_from_how_things_stand_not_from_chance():
    new = bond.openness(dict(BASELINE))
    close = bond.openness({**BASELINE, "trust": 8, "rapport": 8})
    assert close > new                                                           # earned closeness makes it easier
    assert bond.openness({**BASELINE, "arousal": 6}) > new                       # desire throws caution to the wind
    assert bond.openness({**BASELINE, "irritation": 4}) < new                    # annoyance closes her off
    assert bond.openness({**BASELINE, "security": 3}) < new
    assert bond.openness(dict(BASELINE), tension=True) < new
    assert bond.openness(dict(BASELINE)) == new                                  # same situation, same answer


def test_chemistry_is_mostly_average_with_a_few_guarded_and_a_few_quick():
    rolls = [bond.roll_chemistry(random.Random(i)) for i in range(2000)]
    assert 0.6 in rolls and 2.0 in rolls and rolls.count(1.0) > rolls.count(0.6) > rolls.count(2.0)
