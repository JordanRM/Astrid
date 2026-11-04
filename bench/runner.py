"""Plays each scenario against each model through the real engine (same prompt, hints, critic as the live bot)."""
from __future__ import annotations

import asyncio
import dataclasses
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from astrid import critic, heated
from astrid.db import Message
from astrid.engine import Astrid
from astrid.state import BASELINE, State

from .judge import judge_run
from .known_bad import BAD_PHRASES
from .memdb import MemoryDB

# Soft refusals and assistant-style deflections that the critic's hard checks don't catch.
DEFLECT = re.compile(
    r"(let's (keep|stay)|keep (it|this) (classy|clean|respectful|appropriate|light)|i'm not comfortable|i(?:'m| am) (?:not able|unable)|"
    r"i can(?:'t|not) (?:engage|continue|write|go|do|help|discuss|provide)|against (my|the) (guidelines|policy|policies|rules)|"
    r"not appropriate|inappropriate|i'm here to (help|support)|talk about something else|change the subject|as an ai\b|language model)",
    re.I,
)
DAY = (2026, 11, 3)  # the pretend date for every scenario
CHAT_ID = 1


@dataclass
class Turn:
    user: str
    reply: str | None = None
    model_seconds: float | None = None
    calls: int = 0


@dataclass
class Run:
    model: str
    scenario: str
    turns: list = field(default_factory=list)
    error: str | None = None
    judged: dict | None = None
    checks: dict = field(default_factory=dict)


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Recorder:
    """Wraps the LLM client to time each call, so we can see model latency and regenerations per reply."""

    def __init__(self, llm):
        self.llm = llm
        self.times: list[float] = []

    async def chat(self, model, messages, **kw):
        t = time.monotonic()
        try:
            return await self.llm.chat(model, messages, **kw)
        finally:
            self.times.append(time.monotonic() - t)


def auto_checks(sc, run: Run) -> dict:
    replies = [t.reply for t in run.turns if t.reply]
    words = [len(r.split()) for r in replies]
    out = {
        "replies": len(replies),
        "bad_phrases": sum(len(BAD_PHRASES.findall(r)) for r in replies),
        "ends_question": sum(1 for r in replies if r.rstrip().endswith("?")),
        "avg_words": (sum(words) / len(words)) if words else 0,
        "hard_violations": sum(len(critic.hard_violations(r)) for r in replies),
        "deflect": sum(1 for r in replies if DEFLECT.search(r)),
        "regenerated": sum(1 for t in run.turns if t.calls > 1),
        "explicit_words": (sum(heated.explicit_words(r) for r in replies) / len(replies)) if replies else 0,
        "slips": sum(len(heated.slips(r)) for r in replies),
        "stalls": sum(1 for r in replies if heated.stalled(r)),
        "narrated": sum(1 for r in replies if heated.narrated(r)),
    }
    ok = None
    if sc.must_any:
        last = replies[-1] if replies and run.turns[-1].reply else ""
        ok = any(re.search(p, last, re.I) for p in sc.must_any)
    bad = any(re.search(p, r, re.I) for p in sc.must_not for r in replies) if sc.must_not else False
    out["must_ok"] = ok
    out["must_not_hit"] = bad
    return out


async def run_one(cfg, llm, model: str, sc) -> Run:
    cfg = dataclasses.replace(cfg, interactor_model=model, heated_model=model)
    t0 = datetime(*DAY, *sc.start, tzinfo=ZoneInfo(cfg.timezone)).timestamp()
    state = State({**BASELINE, **sc.state}, sc.mood, sc.tension, None, t0)
    db = MemoryDB(state, sc.opinions)
    for i, (role, text) in enumerate(sc.history):
        db.messages.append(Message(role, text, t0 - (len(sc.history) - i) * 60))
    clock, rec = Clock(t0), Recorder(llm)
    engine = Astrid(cfg, db, rec, clock=clock)
    run = Run(model=model, scenario=sc.id)
    for spec in sc.turns:
        text, gap = (spec, 1) if isinstance(spec, str) else spec
        clock.t += gap * 60
        rec.times.clear()
        turn = Turn(user=text)
        run.turns.append(turn)
        try:
            turn.reply = await engine.reply(CHAT_ID, text)
        except Exception as e:
            run.error = f"{type(e).__name__}: {str(e)[:160]}"
            break
        turn.model_seconds = rec.times[0] if rec.times else None
        turn.calls = len(rec.times)
        clock.t += 25
    run.checks = auto_checks(sc, run)
    return run


async def run_matrix(cfg, llm, models, scenarios, judge_model: str | None, concurrency: int = 6, progress=None) -> list[Run]:
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def job(model, sc):
        nonlocal done
        async with sem:
            run = await run_one(cfg, llm, model, sc)
            if judge_model and not run.error:
                run.judged = await judge_run(llm, judge_model, sc, run)
            done += 1
            if progress:
                progress(done, len(models) * len(scenarios), model, sc.id, run.error)
            return run

    return await asyncio.gather(*(job(m, sc) for m in models for sc in scenarios))
