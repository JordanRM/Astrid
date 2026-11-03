from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

from .config import ConfigError, load_config
from .db import Database
from .engine import Astrid
from .llm import LLMError, OpenRouter
from .telegram_bot import Bot, Telegram, TelegramError

USAGE = """usage: python -m astrid [command]
  (none)   run the bot
  migrate  create or update the database tables
  check    verify settings, database, Telegram and the reply model
  flags    list replies you flagged with /meh in Telegram
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
        out = await llm.chat(cfg.interactor_model, [{"role": "user", "content": "say hi in two words"}], max_tokens=20)
        print(f"reply model {cfg.interactor_model}: ok ({out[:40]!r})")
    except LLMError as e:
        ok = False
        print(f"reply model: FAILED ({e})")
    await db.close()
    return 0 if ok else 1


async def _run(cfg) -> None:
    db = Database(cfg.database_url)
    await db.migrate()
    llm = OpenRouter(cfg.openrouter_api_key, cfg.openrouter_base_url)
    engine = Astrid(cfg, db, llm)
    bot = Bot(cfg, db, engine, Telegram(cfg.telegram_token, cfg.telegram_api_base))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.stop)
    try:
        await bot.run()
    finally:
        await db.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    if cmd == "flags":
        async def go():
            db = Database(cfg.database_url)
            await db.migrate()
            rows = await db.list_flags()
            await db.close()
            if not rows:
                print("no flagged replies yet")
            for r in reversed(rows):
                print(f"#{r['id']} [{r['mood']}] jordan: {r['user_text']!r}\n    astrid: {r['reply']!r}\n    note: {r['note'] or '-'}")
        asyncio.run(go())
        return 0
    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
