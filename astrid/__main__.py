from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

from .config import ConfigError, load_config
from .curiosity import Wanderer, format_thoughts
from .search import Brave, SearchError
from .db import Database
from .embed import make_embedder
from .flags import format_flag
from .engine import Astrid
from .memory import Memory, MemoryKeeper, effective_strength, is_hazy, when_text
from .llm import LLMError, OpenRouter
from .net import force_ipv4
from .opinions import format_opinions
from .state import FIELDS, PRESETS, decayed, parse_assignments
from .outreach import Outreach, Plan, quiet_window
from .telegram_bot import Bot, Telegram, TelegramError, send_texts

USAGE = """usage: python -m astrid [command]
  (none)   run the bot
  migrate  create or update the database tables
  check    verify settings, database, Telegram and the reply model
  opinions list the views she has formed, with reasons and changes of mind
  state    show her mood and numbers; set them for testing:  state preset heated  |  state set arousal=8 trust=7
  thoughts list what she's read and thought about on her own time
  wonder   send her off to read something right now (optional: thread, opinion, follow, drift, desire, heat)
  memories list what she remembers, strongest first; or search by meaning:  memories "his brother"
  profile  show what she knows about you; change it:  profile set job="..."  |  profile remove job
  consolidate  run the overnight tidy now (fade weak memories, merge small ones, tidy the profile)
  outreach show whether she'd text first right now; force one:  outreach now follow_up|share|miss|want
  flags    the latest /meh flags in full, with the chat around them and what she was shown (flags 20 for more; add > flags.txt to save)
  reset    forget the conversation and put her back to a first meeting (add --yes)"""


async def _check(cfg) -> int:
    ok = True
    db = Database(cfg.database_url)
    try:
        applied = await db.migrate()
        print(f"database: ok (migrations applied now: {applied or 'none, already current'})")
    except Exception as e:
        ok = False
        print(f"database: FAILED ({type(e).__name__}: {str(e)[:150]})")
    try:
        me = await Telegram(cfg.telegram_token, cfg.telegram_api_base).get_me()
        print(f"telegram: ok (@{me.get('username')})")
    except TelegramError as e:
        ok = False
        print(f"telegram: FAILED ({e})")
    try:
        llm = OpenRouter(cfg.openrouter_api_key, cfg.openrouter_base_url)
        out = await llm.chat(cfg.interactor_model, [{"role": "user", "content": "say hi in two words"}], max_tokens=300)
        print(f"reply model {cfg.interactor_model}: ok ({out[:40]!r})")
    except LLMError as e:
        ok = False
        print(f"reply model: FAILED ({e})")
    if cfg.brave_api_key:
        try:
            hits = await Brave(cfg.brave_api_key).search("lanark village ontario", 2)
            print(f"brave search: ok ({len(hits)} results)")
        except SearchError as e:
            ok = False
            print(f"brave search: FAILED ({e})")
    else:
        print("brave search: off (no BRAVE_API_KEY), so she won't read on her own")
    await db.close()
    return 0 if ok else 1


async def _run(cfg) -> None:
    db = Database(cfg.database_url)
    await db.migrate()
    llm = OpenRouter(cfg.openrouter_api_key, cfg.openrouter_base_url)
    memory = Memory(cfg, db, llm, make_embedder(cfg.embed_model))
    engine = Astrid(cfg, db, llm, memory=memory)
    keeper = MemoryKeeper(cfg, db, memory)
    tg = Telegram(cfg.telegram_token, cfg.telegram_api_base)
    bot = Bot(cfg, db, engine, tg)
    async def send(chat_id, text):
        await send_texts(tg, chat_id, text)
    outreach = Outreach(cfg, db, engine, send, memory)
    wanderer = Wanderer(cfg, db, llm, Brave(cfg.brave_api_key)) if cfg.brave_api_key else None
    if wanderer is None:
        logging.getLogger("astrid").info("no BRAVE_API_KEY: her own reading is off")
    loop = asyncio.get_running_loop()

    def stop():
        bot.stop()
        keeper.stop()
        outreach.stop()
        if wanderer:
            wanderer.stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    wandering = asyncio.create_task(wanderer.loop()) if wanderer else None
    upkeep = asyncio.create_task(keeper.loop())
    reaching = asyncio.create_task(outreach.loop())
    warming = asyncio.create_task(memory.warm())
    try:
        await bot.run()
    finally:
        upkeep.cancel()
        reaching.cancel()
        warming.cancel()
        if wandering:
            wandering.cancel()
        await db.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not os.environ.get("ASTRID_ALLOW_IPV6"):
        force_ipv4()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if cmd == "run":
        asyncio.run(_run(cfg))
        return 0
    if cmd == "migrate":
        async def go():
            db = Database(cfg.database_url)
            print("applied:", await db.migrate() or "nothing, already current")
            await db.close()
        asyncio.run(go())
        return 0
    if cmd == "check":
        return asyncio.run(_check(cfg))
    if cmd == "reset":
        if "--yes" not in sys.argv:
            print("This erases the whole conversation and her mood. Run again with --yes to confirm.")
            return 1
        async def go():
            db = Database(cfg.database_url)
            await db.migrate()
            await db.reset_all(time.time())
            await db.close()
            print("reset done: conversation cleared, state back to baseline")
        asyncio.run(go())
        return 0
    if cmd == "opinions":
        async def go():
            db = Database(cfg.database_url)
            await db.migrate()
            rows = await db.list_opinions(200)
            await db.close()
            print(format_opinions(rows))
        asyncio.run(go())
        return 0
    if cmd == "state":
        sub = sys.argv[2] if len(sys.argv) > 2 else "show"
        async def go():
            db = Database(cfg.database_url)
            await db.migrate()
            now = time.time()
            st = decayed(await db.get_state(), now)
            if sub in ("preset", "set"):
                try:
                    if sub == "preset":
                        name = sys.argv[3] if len(sys.argv) > 3 else ""
                        if name not in PRESETS:
                            raise ValueError(f"presets: {', '.join(PRESETS)}")
                        values, extras = dict(PRESETS[name]), {"mood": {"heated": "wanting you", "close": "warm", "in_love": "soft", "fresh": "neutral"}[name],
                                                              "tension": None, "directive": None}
                        st.values.update(values)
                    else:
                        values, extras = parse_assignments(sys.argv[3:])
                        st.values.update(values)
                except ValueError as e:
                    print(e)
                    await db.close()
                    return
                if "mood" in extras and extras["mood"]:
                    st.mood = extras["mood"]
                if "tension" in extras:
                    st.unresolved_tension = extras["tension"]
                if "directive" in extras:
                    st.directive = extras["directive"]
                await db.save_state(st, now, "manual", {}, "set by hand for testing")
                print("saved. note: arousal and irritation cool within hours, and each exchange moves her a little.")
            await db.close()
            print("mood: " + st.mood + (f" | unresolved: {st.unresolved_tension}" if st.unresolved_tension else "")
                  + (f" | directive: {st.directive}" if st.directive else ""))
            print("  ".join(f"{k} {st.values[k]:.1f}" for k in FIELDS))
        asyncio.run(go())
        return 0
    if cmd == "thoughts":
        async def go():
            db = Database(cfg.database_url)
            await db.migrate()
            rows = await db.list_thoughts(100)
            await db.close()
            from zoneinfo import ZoneInfo
            print(format_thoughts(rows, ZoneInfo(cfg.timezone)))
        asyncio.run(go())
        return 0
    if cmd == "wonder":
        if not cfg.brave_api_key:
            print("set BRAVE_API_KEY in .env first")
            return 1
        force = sys.argv[2] if len(sys.argv) > 2 else None
        async def go():
            db = Database(cfg.database_url)
            await db.migrate()
            w = Wanderer(cfg, db, OpenRouter(cfg.openrouter_api_key, cfg.openrouter_base_url), Brave(cfg.brave_api_key))
            try:
                row = await w.wander(force=force)
            except (SearchError, LLMError, ValueError) as e:
                print(f"didn't work: {e}")
                row = None
            await db.close()
            if row:
                print(f"({row['kind']}) {row['question']}\n  thought: {row['thought']}")
                for src in row["sources"]:
                    print(f"  read: {src['title']} <{src['url']}>")
            elif row is None:
                print("nothing came of it this time (see the log lines above)")
        asyncio.run(go())
        return 0
    if cmd in ("memories", "profile", "consolidate"):
        async def go():
            db = Database(cfg.database_url)
            await db.migrate()
            now = time.time()
            mem = Memory(cfg, db, OpenRouter(cfg.openrouter_api_key, cfg.openrouter_base_url), make_embedder(cfg.embed_model))
            if cmd == "profile":
                args = sys.argv[2:]
                if args and args[0] == "set" and len(args) > 1 and "=" in args[1]:
                    key, _, text = " ".join(args[1:]).partition("=")
                    print(await db.profile_put(key.strip().lower().replace(" ", "_"), text.strip().strip('"'), 6, now))
                elif args and args[0] == "remove" and len(args) > 1:
                    print("removed" if await db.profile_remove(args[1], now) else "no such key")
                await mem.load()
                for f in await db.profile_list():
                    print(f"[{f['key']}] {f['text']}" + (f"   ({len(f['history'])} earlier wording{'s' if len(f['history']) != 1 else ''})" if f["history"] else ""))
            elif cmd == "consolidate":
                print(await mem.consolidate(now))
            else:
                q = " ".join(sys.argv[2:])
                print(f"embedder: {mem.embedder.name}")
                for score, m in await mem.search(q, now, 20):
                    eff = effective_strength(m, now)
                    tag = "permanent" if m["permanent"] else ("hazy" if is_hazy(m, now) else f"{eff:.2f}")
                    print(f"#{m['id']} [{m['kind']}{' explicit' if m['explicit'] else ''}] imp {m['importance']:.0f} {tag} "
                          f"{when_text(m['created_at'], now, mem.tz)}" + (f" sim {score:.2f}" if q else "") + f": {m['text']}")
            await db.close()
        asyncio.run(go())
        return 0
    if cmd == "outreach":
        async def go():
            db = Database(cfg.database_url)
            await db.migrate()
            llm = OpenRouter(cfg.openrouter_api_key, cfg.openrouter_base_url)
            mem = Memory(cfg, db, llm, make_embedder(cfg.embed_model))
            tg = Telegram(cfg.telegram_token, cfg.telegram_api_base)
            async def send(chat_id, text):
                await send_texts(tg, chat_id, text)
            o = Outreach(cfg, db, Astrid(cfg, db, llm, memory=mem), send, mem)
            now = time.time()
            from datetime import datetime
            local = datetime.fromtimestamp(now, o.tz)
            if len(sys.argv) > 3 and sys.argv[2] == "now":
                state = decayed(await db.get_state(), now)
                plans = [p for p in await o.urges(now, state, local, await db.last_user_message_at()) if p.reason == sys.argv[3]]
                plan = plans[0] if plans else Outreach.FORCED.get(sys.argv[3])
                if plan is None:
                    print(f"she has nothing real to {sys.argv[3]} about right now (follow_up needs an open loose end; share needs something she read; try `wonder`)")
                else:
                    print(("(forced for testing, ignoring her mood and the hour) " if not plans else "") +
                          (await o.reach_out(plan) or "she wrote something that didn't pass the checks, so nothing was sent"))
            else:
                await o._load_tide()
                eng = await db.user_message_count(now - 3 * 86400)
                o.tide.for_day(local.date(), min(1.0, 0.6 * eng / 40 + 0.2))
                why = await o._gate(now, local, await db.last_user_message_at())
                print(f"quiet hours now: {quiet_window(local)} | today's tide {o.tide.level:.2f} (cap {o.tide.cap()} texts) | right now: {why or 'free to reach out'}")
                state = decayed(await db.get_state(), now)
                plans = await o.urges(now, state, local, await db.last_user_message_at())
                chances = o.chances(plans, local, state)
                for p, c in zip(plans, chances):
                    print(f"  {p.reason}: urge {p.urge:.2f}, {c * 100:.1f}% at each 5-minute check")
                if not plans:
                    print("  nothing on her mind that would make her text (miss needs 12h of silence, follow_up an open loose end 16h+ old)")
                miss_all = 1.0
                for c in chances:
                    miss_all *= 1 - c
                if why is None and plans and miss_all < 1:
                    n = 0.693 / -__import__("math").log(miss_all) if miss_all > 0 else 1
                    print(f"  typical wait before one of these fires: about {n * 5:.0f} minutes ({n * 5 / 60:.1f} hours)" + (f" [test speed x{o.speed:g}]" if o.speed > 1 else ""))
                print(f"  affection {state.values['affection']:.1f}, security {state.values['security']:.1f}, arousal {state.values['arousal']:.1f}")
            await db.close()
        asyncio.run(go())
        return 0
    if cmd == "flags":
        async def go():
            db = Database(cfg.database_url)
            await db.migrate()
            n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 10
            rows = await db.list_flags(n)
            await db.close()
            if not rows:
                print("no flagged replies yet")
            for r in reversed(rows):
                print(format_flag(r, cfg.timezone))
        asyncio.run(go())
        return 0
    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
