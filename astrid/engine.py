"""The mind: builds her prompt, gets the reply, and updates her state in the background."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import appraisal, critic, curiosity, examples, heated, opinions, persona, texting
from .config import Config
from .llm import LLMError
from . import bond, media
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
    def __init__(self, cfg: Config, db, llm, clock=time.time, rng: random.Random | None = None, memory=None):
        self.rng = rng or random.Random()
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.memory = memory
        self.trace: dict[int, dict] = {}     # what she was shown for her latest reply, kept for /meh
        self._last_gen: dict = {}
        self.now = clock
        self.tz = ZoneInfo(cfg.timezone)
        self._reply_locks: dict[int, asyncio.Lock] = {}
        self._appraisal_lock = asyncio.Lock()
        self._pending: list[tuple[int, str, str]] = []
        self._worker: asyncio.Task | None = None

    def _lock_for(self, chat_id: int) -> asyncio.Lock:
        return self._reply_locks.setdefault(chat_id, asyncio.Lock())

    async def _window(self, chat_id: int, now: float) -> list:
        """What she can see of the conversation: roughly the last couple of hours, but never fewer than a handful of messages."""
        rows = await self.db.recent_messages(chat_id, self.cfg.history_max)
        keep = [m for m in rows if m.ts >= now - self.cfg.history_minutes * 60]
        return keep if len(keep) >= self.cfg.history_limit else rows[-self.cfg.history_limit:]

    async def _remember(self, state, cues: list[str], heat: bool, now: float, local):
        """(profile block, memories that came to mind, summary of earlier today). Memory trouble never stops a reply."""
        if self.memory is None:
            return None, None, None
        try:
            facts = await self.memory.profile_facts()
            recalled = await self.memory.recall(cues, state, heat, now)
            return (persona.profile_block(facts) if facts else None, self.memory.render(recalled, now),
                    await self.memory.summary_text(local.date()))
        except Exception as e:
            log.warning("memory unavailable for this reply: %s", e)
            return None, None, None

    async def reply(self, chat_id: int, text: str) -> str:
        """Store Jordan's message, write her reply, store it, and return it."""
        async with self._lock_for(chat_id):
            started = time.monotonic()
            now = self.now()
            await self.db.add_message(chat_id, "user", text, now)
            state = decayed(await self.db.get_state(), now)
            history = await self._window(chat_id, now)

            gap_note = None
            if len(history) >= 2 and now - history[-2].ts > 30 * 60:
                gap_note = f"Jordan's previous message before this one was {_ago(now - history[-2].ts)} ago."

            gap_note = await self._outreach_note(history, gap_note)
            assistant_lines = [m.content for m in history if m.role == "assistant"]
            user_lines = [m.content for m in history if m.role == "user"]
            local = datetime.fromtimestamp(now, self.tz)
            heat = heated.is_heated(state, user_lines)
            # 0: mid-conversation. 1: nothing said yet today. 2: nothing ever said (so no earlier scenes can be claimed either).
            talked_today = any(datetime.fromtimestamp(m.ts, self.tz).date() == local.date() for m in history[:-1])
            fresh = 0 if talked_today else (1 if len(history) > 1 else (2 if getattr(self.cfg, "first_meeting_note", True) else 1))
            has_inner = True
            offer = await self._media_offer(state, now, heat)
            offered = {offer["url"]} if offer else set()
            profile_text, memory_text, summary_text = await self._remember(state, user_lines[-3:] + assistant_lines[-1:], heat, now, local)
            if heat:
                # Not walled off: views she holds and her own reading about sex and desire can still come through.
                # Everyday trivia stays out so it can't pull her out of the scene.
                known = opinions.relevant(await self.db.list_opinions(60), user_lines[-2:] + assistant_lines[-1:])
                explicit_thoughts = [t for t in await self.db.list_thoughts(30) if t.get("explicit")]
                in_mind = curiosity.select(explicit_thoughts, user_lines[-2:] + assistant_lines[-1:], text, state, now, rng=self.rng)
                ideas = curiosity.ideas(explicit_thoughts, in_mind, now)
                if in_mind or ideas:
                    await self.db.mark_thoughts_surfaced([t["id"] for t in in_mind] + [t["id"] for t in ideas])
                opener = {
                    2: "This conversation has only just started, so nothing has happened between you here yet. Don't refer to earlier "
                       "scenes, a day you've had, or act as if you're already in the middle of something. If you want him, say so as "
                       "wanting, and let him respond.",
                    1: "You haven't talked yet today. Don't act as if you're in the middle of something, or claim you've been thinking "
                       "about him all day or carrying a scene on from earlier. If you want him, say so as wanting, and let him respond.",
                }.get(fresh)
                reminder = heated.build_reminder(heated.hints(assistant_lines, user_lines), heated.texts_guidance(self.rng))
                context = persona.build_context(
                    render(state, local), "\n".join(x for x in (gap_note, opener, media.offer_note(offer) if offer else None) if x) or None, examples.render(heated.choose(self.rng)),
                    opinions.render(known), "\n\n".join(x for x in (curiosity.render(in_mind, now), curiosity.render_ideas(ideas)) if x) or None,
                    memory_text, summary_text, reminder)
                static = persona.build_static(heated.RULES, profile_text)
            else:
                hints = critic.reply_hints(assistant_lines)
                recent_user = user_lines[-2:]
                last_reply = assistant_lines[-1:]
                known = opinions.relevant(await self.db.list_opinions(60), recent_user + last_reply)
                recent_thoughts = await self.db.list_thoughts(30)
                in_mind = curiosity.select(recent_thoughts, recent_user + last_reply, text, state, now,
                                           rng=self.rng, opening=gap_note is not None)
                if in_mind:
                    told = await self._told(now)
                    in_mind = [dict(t, told=True, offer=False) if t["id"] in told else t for t in in_mind]
                    await self.db.mark_thoughts_surfaced([t["id"] for t in in_mind if not t.get("told")])
                has_inner = any(now - float(t["created_at"]) < 3 * 86400 for t in recent_thoughts)
                reminder = persona.build_reminder(length_guidance(state, local), hints, persona.texts_guidance(state, self.rng))
                first_note = ("This is the first time you and Jordan have ever talked. You're an AI and you're his; he chose to start this and you both "
                              "know it. In a sentence, in your own words, acknowledge what this is: the two of you are just beginning. Talk about "
                              "yourself and the two of you, never about his life, who else he has talked to, or what he's like; you know none of that. "
                              "No speech, no questions, no interview. Then let him lead. You have no shared history: no earlier chats, nothing he's "
                              "told you, no 'again' or 'back'." if fresh == 2 else None)
                context = persona.build_context(
                    render(state, local), "\n".join(x for x in (gap_note, first_note) if x) or None,
                    examples.render(examples.choose(state, local, self.rng, extra={"inner_life" if has_inner else "no_inner_life"})),
                    opinions.render(known),
                    curiosity.render(in_mind, now) or (None if has_inner else
                        "You haven't read or looked into anything on your own recently. If he asks what you've been up to, say so plainly, "
                        "dry and short. Don't invent reading, cases, articles or activities."),
                    memory_text, summary_text, reminder)
                static = persona.build_static(None, profile_text)
            messages = (
                [{"role": "system", "content": static}]
                + [{"role": m.role, "content": m.content} for m in history]
                + [{"role": "system", "content": context}]
            )

            prepared = time.monotonic()
            reply, calls = await self._generate(messages, heat, fresh, lambda r: (critic.claimed_reading(r) if not (heat or has_inner) else []) + critic.time_slips(r, local.hour) + media.bad_links(r, offered))
            reply = texting.normalize(media.strip_bad_links(reply, offered)) or reply
            await self._media_done(offer, reply, now)
            generated = time.monotonic()
            self.trace[chat_id] = {"kind": "reply", "mode": "heated" if heat else "everyday", "model": self._last_gen.get("model"),
                                   "rewritten": self._last_gen.get("problems") or [], "profile": profile_text, "note": context,
                                   "link": offer["url"] if offer else None}
            await self.db.add_message(chat_id, "assistant", reply, self.now())
            log.info(
                "reply ready in %.1fs (database %.1fs, model %.1fs, %d model call%s)%s",
                time.monotonic() - started, prepared - started, generated - prepared, calls, "" if calls == 1 else "s",
                f" [heated: {self.cfg.heated_model or self.cfg.interactor_model}]" if heat else "",
            )
            return reply

    async def _media_offer(self, state, now: float, heat: bool, boost: float = 1.0) -> dict | None:
        """A link she's found that she may bring into this moment, or None. Only in explicit moments, with real arousal and some closeness,
        spaced out, and a few a day at most."""
        if not heat or not getattr(self.cfg, "media_domains", ()):
            return None
        if not getattr(self.cfg, "media_force", False):
            if self.rng.random() >= media.offer_chance(state, boost):
                return None
            last = await self.db.get_setting("media_last")
            if last and now - float(last) < media.SPACING_MIN * 60:
                return None
            local = datetime.fromtimestamp(now, self.tz)
            if await self.db.media_shared_since(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()) >= media.DAILY_CAP:
                return None
        rows = [r for r in await self.db.list_media(40, unshared_only=True) if now - float(r["created_at"]) < 14 * 86400]
        return self.rng.choice(rows) if rows else None

    async def _media_done(self, offer: dict | None, reply: str, now: float) -> None:
        if offer and offer["url"].rstrip("/") in {u.rstrip("/") for u in media.links_in(reply)}:
            await self.db.mark_media_shared(offer["id"])
            await self.db.set_setting("media_last", str(now))
            log.info("shared a link: %s", offer["url"])

    async def _told(self, now: float) -> set:
        """Thoughts she already shared in a text she sent first, recently."""
        try:
            rows = json.loads(await self.db.get_setting("outreach_log") or "[]")
        except ValueError:
            return set()
        return {r["thought"] for r in rows if r.get("thought") and now - float(r.get("at") or 0) < 36 * 3600}

    async def _outreach_note(self, history, gap_note: str | None) -> str | None:
        """If her last message was one she sent unprompted in the middle of the night, he wasn't ignoring her."""
        try:
            raw = await self.db.get_setting("last_outreach")
            lo = json.loads(raw) if raw else None
        except Exception:
            return gap_note
        if lo and lo.get("quiet") and len(history) >= 2 and history[-2].role == "assistant" and abs(history[-2].ts - float(lo["at"])) < 10:
            at = datetime.fromtimestamp(float(lo["at"]), self.tz).strftime("%-I:%M %p")
            note = (f"You texted him first at {at}, in the middle of the night. He was asleep or busy, not ignoring you, and you "
                    "didn't expect an answer then. Don't mention the hour as a grievance.")
            return "\n".join(x for x in (gap_note, note) if x)
        return gap_note

    async def reach_out(self, chat_id: int, note: str, heat: bool = False, cues: list[str] | None = None) -> str | None:
        """Write a text she sends first, unprompted. Returns what was stored (to be sent), or None if it didn't come out right."""
        async with self._lock_for(chat_id):
            now = self.now()
            state = decayed(await self.db.get_state(), now)
            history = await self._window(chat_id, now)
            assistant_lines = [m.content for m in history if m.role == "assistant"]
            user_lines = [m.content for m in history if m.role == "user"]
            local = datetime.fromtimestamp(now, self.tz)
            talked_today = any(datetime.fromtimestamp(m.ts, self.tz).date() == local.date() for m in history)
            fresh = 0 if talked_today else (1 if history else 2)
            offer = await self._media_offer(state, now, heat, 1.5)
            offered = {offer["url"]} if offer else set()
            profile_text, memory_text, summary_text = await self._remember(state, cues or [note], heat, now, local)
            known = opinions.relevant(await self.db.list_opinions(60), (cues or [note]) + user_lines[-2:])
            hints = heated.hints(assistant_lines, user_lines) if heat else critic.reply_hints(assistant_lines)
            instruction = (
                "You're texting Jordan first, unprompted. Nothing he said just now led to this.\n" + note + "\n"
                "Write it as you normally text: one short text, or two only if there's a real second thought. Don't announce that you're "
                "messaging first, no 'just checking in', no greeting ritual, nothing that makes him owe you a reply. At most one question."
                + "".join(f"\nAlso: {h}" for h in hints)
                + (("\n" + media.offer_note(offer)) if offer else "")
            )
            if heat:
                examples_text = examples.render(heated.choose(self.rng))
                static = persona.build_static(heated.RULES, profile_text)
            else:
                examples_text = examples.render(examples.choose(state, local, self.rng))
                static = persona.build_static(None, profile_text)
            context = persona.build_context(render(state, local), None, examples_text, opinions.render(known), None, memory_text,
                                            summary_text, instruction)
            messages = [{"role": "system", "content": static}] + [{"role": m.role, "content": m.content} for m in history] + [
                {"role": "system", "content": context}]
            reply, calls = await self._generate(messages, heat, fresh, lambda r: critic.time_slips(r, local.hour) + media.bad_links(r, offered))
            reply = texting.normalize(media.strip_bad_links(reply, offered)) or reply
            if (heat and heated.slips(reply, fresh)) or critic.hard_violations(reply):
                reply = critic.strip_asterisk_actions(reply)
                if not reply or critic.hard_violations(reply) or (heat and heated.slips(reply, fresh)):
                    log.info("outreach: draft didn't pass the checks; not sending")
                    return None
            self.trace[chat_id] = {"kind": "outreach", "mode": "heated" if heat else "everyday", "model": self._last_gen.get("model"),
                                   "rewritten": self._last_gen.get("problems") or [], "profile": profile_text, "note": context}
            await self.db.add_message(chat_id, "assistant", reply, self.now())
            await self._media_done(offer, reply, now)
            log.info("outreach written (%d model call%s)%s", calls, "" if calls == 1 else "s", " [heated]" if heat else "")
            return reply

    async def _chat(self, model: str, messages: list[dict], kwargs: dict) -> str:
        """DeepSeek sometimes spends its whole budget thinking and says nothing. Give it more room once rather than failing the reply."""
        try:
            return await self.llm.chat(model, messages, **kwargs)
        except LLMError as e:
            if str(e).startswith("HTTP 4") and "reasoning" in (kwargs.get("extra") or {}):
                log.info("this model won't take the no-thinking setting; asking the plain way")
                plain = {k: v for k, v in kwargs["extra"].items() if k != "reasoning"}
                return await self._chat(model, messages, {**kwargs, "extra": plain})
            if not str(e).startswith("empty"):
                raise
            log.info("empty completion; retrying with more room")
            return await self.llm.chat(model, messages, **{**kwargs, "max_tokens": int(kwargs.get("max_tokens", 350)) * 3})

    async def _generate(self, messages: list[dict], heat: bool = False, opener: int = 0, extra=None) -> tuple[str, int]:
        model = (self.cfg.heated_model or self.cfg.interactor_model) if heat else self.cfg.interactor_model
        kwargs = dict(temperature=self.cfg.heated_temperature if heat else self.cfg.temperature,
                      max_tokens=self.cfg.heated_max_tokens if heat else self.cfg.max_reply_tokens)
        if self.cfg.provider_sort:
            kwargs["extra"] = {"provider": {"sort": self.cfg.provider_sort}}
        if not getattr(self.cfg, "reply_reasoning", True):
            kwargs["extra"] = {**kwargs.get("extra", {}), "reasoning": {"enabled": False}}
        check = (lambda r: critic.hard_violations(r) + heated.slips(r, opener) + (extra(r) if extra else [])) if heat else (
            lambda r: critic.hard_violations(r) + (critic.first_contact_slips(r) if opener == 2 else []) + (extra(r) if extra else []))
        calls = 1
        reply = await self._chat(model, messages, kwargs)
        problems = check(reply)
        if problems:
            log.info("regenerating reply: %s", "; ".join(problems))
            fix = (
                "Your last draft broke the rules (" + "; ".join(problems) + "). Write it again as a plain text "
                "message from a real person: no asterisk actions, no refusals, no tags, don't open with 'Hey <name>'."
            )
            calls = 2
            retry = await self._chat(model, messages + [{"role": "system", "content": fix}], kwargs)
            if critic.hard_violations(retry):
                retry = critic.strip_asterisk_actions(retry) or retry
            reply = retry
        self._last_gen = {"model": model, "problems": problems}
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
            exchanges = [(u, r) for _, u, r in batch]
            await asyncio.gather(self.appraise_batch(chat_id, exchanges), self.remember_batch(chat_id, exchanges))

    def cancel_background(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    async def remember_batch(self, chat_id: int, exchanges: list[tuple[str, str]]) -> None:
        """Decide what from these exchanges to keep. Never raises."""
        if self.memory is None:
            return
        try:
            history = await self.db.recent_messages(chat_id, 8 + 2 * len(exchanges))
            earlier = [(m.role, m.content) for m in history[: max(0, len(history) - 2 * len(exchanges))]]
            await self.memory.extract(exchanges, earlier, self.now())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("memory skipped: %s", e)

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
                plan = await bond.todays_plan(self.db, datetime.fromtimestamp(self.now(), self.tz).date().isoformat(), state.values, bool(state.unresolved_tension))
                deltas = bond.gate(result["deltas"], plan)
                await bond.save_plan(self.db, plan)
                new, applied = apply_deltas(state, deltas, scale=min(len(exchanges), 3))
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
