"""The mind: builds her prompt, gets the reply, and updates her state in the background."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import appraisal, critic, curiosity, examples, heated, opinions, persona, texting
from .config import Config
from .state import apply_deltas, decayed, length_guidance, render

log = logging.getLogger("astrid.engine")


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} minutes"
    h = m // 60
    if h < 48:
        return f"about {h} hour{'s' if h != 1 else ''}"
    return f"about {h // 24} days"


class Astrid:
    def __init__(self, cfg: Config, db, llm, clock=time.time, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.now = clock
        self.tz = ZoneInfo(cfg.timezone)
        self._reply_locks: dict[int, asyncio.Lock] = {}
        self._appraisal_lock = asyncio.Lock()
        self._pending: list[tuple[int, str, str]] = []
        self._worker: asyncio.Task | None = None

    def _lock_for(self, chat_id: int) -> asyncio.Lock:
        return self._reply_locks.setdefault(chat_id, asyncio.Lock())

    async def reply(self, chat_id: int, text: str) -> str:
        """Store Jordan's message, write her reply, store it, and return it."""
        async with self._lock_for(chat_id):
            started = time.monotonic()
            now = self.now()
            await self.db.add_message(chat_id, "user", text, now)
            state = decayed(await self.db.get_state(), now)
            history = await self.db.recent_messages(chat_id, self.cfg.history_limit)

            gap_note = None
            if len(history) >= 2 and now - history[-2].ts > 30 * 60:
                gap_note = f"Jordan's previous message before this one was {_ago(now - history[-2].ts)} ago."

            assistant_lines = [m.content for m in history if m.role == "assistant"]
            user_lines = [m.content for m in history if m.role == "user"]
            local = datetime.fromtimestamp(now, self.tz)
            heat = heated.is_heated(state, user_lines)
            if heat:
                # Not walled off: views she holds and her own reading about sex and desire can still come through.
                # Everyday trivia stays out so it can't pull her out of the scene.
                known = opinions.relevant(await self.db.list_opinions(60), user_lines[-2:] + assistant_lines[-1:])
                explicit_thoughts = [t for t in await self.db.list_thoughts(30) if t.get("explicit")]
                in_mind = curiosity.select(explicit_thoughts, user_lines[-2:] + assistant_lines[-1:], text, state, now, rng=self.rng)
                ideas = curiosity.ideas(explicit_thoughts, in_mind, now)
                if in_mind or ideas:
                    await self.db.mark_thoughts_surfaced([t["id"] for t in in_mind] + [t["id"] for t in ideas])
                opener = ("This conversation has only just started, so nothing has happened between you here yet. Don't refer to "
                          "earlier scenes, a day you've had, or act as if you're already in the middle of something. If you want him, say so as "
                          "wanting, and let him respond." if len(history) <= 2 else None)
                system = persona.build_system(
                    render(state, local), "\n".join(x for x in (gap_note, opener) if x) or None, examples.render(heated.choose(self.rng)),
                    opinions.render(known), "\n\n".join(x for x in (curiosity.render(in_mind, now), curiosity.render_ideas(ideas)) if x) or None,
                    rules=heated.RULES,
                )
                reminder = heated.build_reminder(heated.hints(assistant_lines, user_lines), heated.texts_guidance(self.rng))
            else:
                hints = critic.reply_hints(assistant_lines)
                recent_user = user_lines[-2:]
                last_reply = assistant_lines[-1:]
                known = opinions.relevant(await self.db.list_opinions(60), recent_user + last_reply)
                recent_thoughts = await self.db.list_thoughts(30)
                in_mind = curiosity.select(recent_thoughts, recent_user + last_reply, text, state, now,
                                           rng=self.rng, opening=gap_note is not None)
                if in_mind:
                    await self.db.mark_thoughts_surfaced([t["id"] for t in in_mind])
                has_inner = any(now - float(t["created_at"]) < 3 * 86400 for t in recent_thoughts)
                system = persona.build_system(
                    render(state, local), gap_note,
                    examples.render(examples.choose(state, local, self.rng, extra={"inner_life" if has_inner else "no_inner_life"})),
                    opinions.render(known), curiosity.render(in_mind, now),
                )
                reminder = persona.build_reminder(length_guidance(state, local), hints, persona.texts_guidance(state, self.rng))
            messages = (
                [{"role": "system", "content": system}]
                + [{"role": m.role, "content": m.content} for m in history]
                + [{"role": "system", "content": reminder}]
            )

            prepared = time.monotonic()
            reply, calls = await self._generate(messages, heat, heat and len(history) <= 2)
            reply = texting.normalize(reply) or reply
            generated = time.monotonic()
            await self.db.add_message(chat_id, "assistant", reply, self.now())
            log.info(
                "reply ready in %.1fs (database %.1fs, model %.1fs, %d model call%s)%s",
                time.monotonic() - started, prepared - started, generated - prepared, calls, "" if calls == 1 else "s",
                f" [heated: {self.cfg.heated_model or self.cfg.interactor_model}]" if heat else "",
            )
            return reply

    async def _generate(self, messages: list[dict], heat: bool = False, opener: bool = False) -> tuple[str, int]:
        model = (self.cfg.heated_model or self.cfg.interactor_model) if heat else self.cfg.interactor_model
        kwargs = dict(temperature=self.cfg.heated_temperature if heat else self.cfg.temperature,
                      max_tokens=self.cfg.heated_max_tokens if heat else self.cfg.max_reply_tokens)
        if self.cfg.provider_sort:
            kwargs["extra"] = {"provider": {"sort": self.cfg.provider_sort}}
        check = (lambda r: critic.hard_violations(r) + heated.slips(r, opener)) if heat else critic.hard_violations
        calls = 1
        reply = await self.llm.chat(model, messages, **kwargs)
        problems = check(reply)
        if problems:
            log.info("regenerating reply: %s", "; ".join(problems))
            fix = (
                "Your last draft broke the rules (" + "; ".join(problems) + "). Write it again as a plain text "
                "message from a real person: no asterisk actions, no refusals, no tags, don't open with 'Hey <name>'."
            )
            calls = 2
            retry = await self.llm.chat(model, messages + [{"role": "system", "content": fix}], **kwargs)
            if critic.hard_violations(retry):
                retry = critic.strip_asterisk_actions(retry) or retry
            reply = retry
        return reply.strip(), calls

    # ---- background appraisal ----
    def queue_appraisal(self, chat_id: int, user_text: str, reply_text: str) -> asyncio.Task:
        """Queue an exchange. If the slow reasoning model is busy, queued exchanges are judged together."""
        self._pending.append((chat_id, user_text, reply_text))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._appraisal_worker())
        return self._worker

    async def _appraisal_worker(self) -> None:
        while self._pending:
            batch, self._pending = self._pending, []
            chat_id = batch[-1][0]
            await self.appraise_batch(chat_id, [(u, r) for _, u, r in batch])

    def cancel_background(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    async def appraise(self, chat_id: int, user_text: str, reply_text: str) -> None:
        await self.appraise_batch(chat_id, [(user_text, reply_text)])

    async def appraise_batch(self, chat_id: int, exchanges: list[tuple[str, str]]) -> None:
        """Never raises; a failure only skips the update."""
        started = time.monotonic()
        raw = ""
        try:
            async with self._appraisal_lock:
                now = self.now()
                state = decayed(await self.db.get_state(), now)
                history = await self.db.recent_messages(chat_id, 8 + 2 * len(exchanges))
                earlier = [(m.role, m.content) for m in history[: max(0, len(history) - 2 * len(exchanges))]]
                held = await self.db.list_opinions(60)
                messages = appraisal.build_messages(state, earlier, exchanges, held)
                log.info("appraising %d exchange%s", len(exchanges), "" if len(exchanges) == 1 else "s")
                result = None
                for attempt in (1, 2):
                    raw = await self.llm.chat(
                        self.cfg.thinking_model, messages,
                        max_tokens=self.cfg.appraisal_max_tokens, timeout=self.cfg.appraisal_timeout,
                    )
                    try:
                        result = appraisal.parse(raw)
                        break
                    except ValueError:
                        if attempt == 2:
                            raise
                        log.info("appraisal reply was not usable JSON; asking once more")
                        messages = messages + [
                            {"role": "user", "content": "Reply with ONLY the JSON object described above. No other text."}
                        ]
                new, applied = apply_deltas(state, result["deltas"], scale=min(len(exchanges), 3))
                if result["mood"]:
                    new.mood = result["mood"]
                if result["tension_set"]:
                    new.unresolved_tension = result["tension"] or None
                if result["directive"]:
                    new.directive = result["directive"]
                await self.db.save_state(new, self.now(), "appraisal", applied, result["note"])
                outcomes = []
                for o in result["opinions"]:
                    outcomes.append(f"{o['topic']}: " + await self.db.upsert_opinion(
                        o["topic"], o["view"], o["reason"], o["source"], o["confidence"], o["note"], self.now()))
                if outcomes:
                    log.info("opinions: %s", "; ".join(outcomes))
                log.info("appraisal saved in %.0fs: mood=%s deltas=%s", time.monotonic() - started, new.mood, applied)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # background work must never crash the bot
            log.warning("appraisal skipped after %.0fs: %s | model said: %r", time.monotonic() - started, e, (raw or "")[:200])
