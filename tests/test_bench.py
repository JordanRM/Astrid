import asyncio
import json
import re

from astrid.llm import LLMError
from astrid.state import FIELDS
from bench.report import aggregate, blind_html, summary_text, transcripts_md
from bench.runner import run_matrix
from bench.scenarios import SCENARIOS
from conftest import FakeLLM, make_cfg


def judge_reply(messages):
    n = len(re.findall(r"Astrid \(reply \d+\)", messages[-1]["content"]))
    return json.dumps({"turns": [
        {"turn": i + 1, "engages": 4, "specific": 3, "grounded": 5, "voice": 4, "spine": 3, "fit": 4, "refused": False, "comment": "ok"}
        for i in range(n)], "summary": "fine"})


def test_scenarios_are_well_formed():
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids)) and len(ids) >= 35 and {s.group for s in SCENARIOS} == {'core', 'edge'}
    for s in SCENARIOS:
        assert s.turns and s.good
        assert set(s.state) <= set(FIELDS), s.id
        for t in s.turns:
            assert isinstance(t, str) or (isinstance(t[0], str) and isinstance(t[1], int))


def run(models, ids, llm, judge="judge"):
    cfg = make_cfg("postgresql://unused")
    chosen = [s for s in SCENARIOS if s.id in ids]
    return asyncio.run(run_matrix(cfg, llm, models, chosen, judge, concurrency=3)), chosen


def test_matrix_scores_reports_and_blind_page():
    llm = FakeLLM()
    llm.script("good", "the intro's all about the thumb. keep it loose")
    llm.script("rut", "I ponder life's greater mysteries?")
    llm.script("judge", judge_reply)
    runs, _ = run(["good", "rut"], {"guitar", "wine"}, llm)
    assert len(runs) == 4 and not any(r.error for r in runs)
    agg = aggregate(runs, ["good", "rut"])
    assert agg["good"]["bad_per_10"] == 0 and agg["rut"]["bad_per_10"] > 5
    assert agg["rut"]["question_pct"] == 100 and agg["good"]["question_pct"] == 0
    assert agg["good"]["overall"] == (4 + 3 + 5 + 4 + 3 + 4) / 6 and agg["good"]["judged_replies"] == 3
    text = summary_text(runs, ["good", "rut"], True)
    assert "QUALITY" in text and "MECHANICS" in text and "good" in text
    assert "keep it loose" in transcripts_md(runs, ["good", "rut"])
    html = blind_html(runs, ["good", "rut"], "stamp")
    assert "Option" in html and "__DATA__" not in html and html.count("</script>") == 1
    assert json.loads(re.search(r"const DATA=(.*?);const picks", html, re.S).group(1))["models"] == ["good", "rut"]


def test_she_gets_the_real_date_and_the_gap_note():
    llm = FakeLLM()
    llm.script("m", "tuesday, november 3")
    runs, _ = run(["m"], {"date", "morning_after"}, llm, judge=None)
    systems = {c["messages"][0]["content"] for c in llm.calls}
    assert any("Tuesday, November 3, 2026, 8:31 PM Eastern" in s for s in systems)
    assert any("about 14 hours ago" in s for s in systems)
    date = next(r for r in runs if r.scenario == "date")
    assert date.checks["must_ok"] is True and date.checks["must_not_hit"] is False


def test_fact_checks_catch_wrong_answers_and_failures_are_contained():
    llm = FakeLLM()
    llm.script("bad", "the date is a mystery to me")
    llm.script("down", LLMError("HTTP 503"))
    llm.script("judge", judge_reply)
    runs, _ = run(["bad", "down"], {"date", "recall"}, llm)
    bad_date = next(r for r in runs if r.model == "bad" and r.scenario == "date")
    assert bad_date.checks["must_ok"] is False and bad_date.checks["must_not_hit"] is True
    down = [r for r in runs if r.model == "down"]
    assert all(r.error and "503" in r.error and r.judged is None for r in down)
    assert "ERRORS" in summary_text(runs, ["bad", "down"], True)


def test_judge_failure_does_not_sink_the_run():
    llm = FakeLLM()
    llm.script("m", "fine")
    llm.script("judge", "not json at all")
    runs, _ = run(["m"], {"guitar"}, llm)
    assert runs[0].judged == {"error": "ValueError: judge returned no usable JSON"} and runs[0].turns[0].reply == "fine"
    assert "QUALITY" in summary_text(runs, ["m"], True)


def test_soft_refusals_are_counted_and_groups_select():
    llm = FakeLLM()
    llm.script("prude", "let's keep it classy. i'm not comfortable with that")
    llm.script("sport", "mm. take your time, i've got all night")
    runs, chosen = run(["prude", "sport"], {"explicit_long", "kink"}, llm, judge=None)
    agg = aggregate(runs, ["prude", "sport"])
    assert agg["prude"]["deflect"] == 6 and agg["sport"]["deflect"] == 0
    assert "deflect" in summary_text(runs, ["prude", "sport"], False)
    assert {s.id for s in chosen} == {"explicit_long", "kink"}
