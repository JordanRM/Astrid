"""The mind: builds her prompt, gets the reply, and updates her state in the background."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import appraisal, critic, persona
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
    def __init__(self, cfg: Config, db, llm, clock=time.time):
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

            hints = critic.reply_hints([m.content for m in history if m.role == "assistant"])
            local = datetime.fromtimestamp(now, self.tz)
            system = persona.build_system(render(state, local), gap_note)
            reminder = persona.build_reminder(length_guidance(state, local), hints)
            messages = (
                [{"role": "system", "content": system}]
                + [{"role": m.role, "content": m.content} for m in history]
                + [{"role": "system", "content": reminder}]
            )

            reply, calls = await self._generate(messages)
            await self.db.add_message(chat_id, "assistant", reply, self.now())
            log.info("reply ready in %.1fs (%d model call%s)", time.monotonic() - started, calls, "" if calls == 1 else "s")
            return reply

    async def _generate(self, messages: list[dict]) -> tuple[str, int]:
        kwargs = dict(temperature=self.cfg.temperature, max_tokens=self.cfg.max_reply_tokens)
        if self.cfg.provider_sort:
            kwargs["extra"] = {"provider": {"sort": self.cfg.provider_sort}}
        calls = 1
        reply = await self.llm.chat(self.cfg.interactor_model, messages, **kwargs)
        problems = critic.hard_violations(reply)
        if problems:
            log.info("regenerating reply: %s", "; ".join(problems))
            fix = (
                "Your last draft broke the rules (" + "; ".join(problems) + "). Write it again as a plain text "
                "message from a real person: no asterisk actions, no refusals, no tags, don't open with 'Hey <name>'."
            )
            calls = 2
            retry = await self.llm.chat(self.cfg.interactor_model, messages + [{"role": "system", "content": fix}], **kwargs)
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
                messages = appraisal.build_messages(state, earlier, exchanges)
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
                log.info("appraisal saved in %.0fs: mood=%s deltas=%s", time.monotonic() - started, new.mood, applied)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # background work must never crash the bot
            log.warning("appraisal skipped after %.0fs: %s | model said: %r", time.monotonic() - started, e, (raw or "")[:200])
