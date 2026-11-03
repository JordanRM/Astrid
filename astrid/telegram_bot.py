"""Telegram front end using the plain Bot API (long polling). Text only for now."""
from __future__ import annotations

import asyncio
import logging

import requests

log = logging.getLogger("astrid.telegram")


class TelegramError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


class Telegram:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org"):
        self._url = f"{api_base.rstrip('/')}/bot{token}"

    def _call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict | list | bool:
        try:
            r = requests.post(f"{self._url}/{method}", json=params or {}, timeout=timeout)
        except requests.RequestException as e:
            raise TelegramError(f"{method}: network {type(e).__name__}") from None
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {r.status_code}, not JSON", r.status_code) from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'failed')}", data.get("error_code", r.status_code))
        return data["result"]

    async def call(self, method: str, params: dict | None = None, timeout: float = 20.0):
        return await asyncio.to_thread(self._call, method, params, timeout)

    async def get_me(self):
        return await self.call("getMe")

    async def get_updates(self, offset: int | None, poll_seconds: int = 30):
        params = {"timeout": poll_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", params, timeout=poll_seconds + 15)

    async def send_message(self, chat_id: int, text: str):
        return await self.call("sendMessage", {"chat_id": chat_id, "text": text})

    async def typing(self, chat_id: int):
        try:
            await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
        except TelegramError:
            pass


class Bot:
    def __init__(self, cfg, db, engine, tg: Telegram):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self.tg = tg
        self._tasks: set[asyncio.Task] = set()
        self._stop = False

    def stop(self):
        self._stop = True

    async def _owner_id(self) -> int | None:
        if self.cfg.owner_telegram_id:
            return self.cfg.owner_telegram_id
        stored = await self.db.get_setting("owner_telegram_id")
        return int(stored) if stored else None

    async def _skip_backlog(self) -> int | None:
        """First start: don't answer messages that piled up while no bot was running."""
        stored = await self.db.get_setting("telegram_offset")
        if stored:
            return int(stored)
        try:
            last = await self.tg.call("getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": ["message"]})
        except TelegramError as e:
            log.warning("could not skip backlog: %s", e)
            return None
        offset = last[-1]["update_id"] + 1 if last else None
        if offset:
            await self.db.set_setting("telegram_offset", str(offset))
        return offset

    async def _typing_loop(self, chat_id: int):
        try:
            while True:
                await self.tg.typing(chat_id)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    async def handle_message(self, msg: dict):
        chat = msg.get("chat") or {}
        sender = (msg.get("from") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat.get("type") != "private" or not sender or not text or text.startswith("/"):
            return
        owner = await self._owner_id()
        if owner is None:
            await self.db.set_setting("owner_telegram_id", str(sender))
            owner = sender
            log.info("claimed owner: telegram user %s", sender)
        if sender != owner:
            log.info("ignored message from non-owner %s", sender)
            return

        chat_id = chat["id"]
        typing = asyncio.create_task(self._typing_loop(chat_id))
        try:
            reply = await self.engine.reply(chat_id, text)
        except Exception as e:
            log.exception("reply failed")
            typing.cancel()
            try:
                await self.tg.send_message(chat_id, "(i glitched, say that again)")
            except TelegramError:
                pass
            return
        finally:
            typing.cancel()
        try:
            await self.tg.send_message(chat_id, reply)
        except TelegramError as e:
            log.error("could not send reply: %s", e)
            return
        task = asyncio.create_task(self.engine.appraise(chat_id, text, reply))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self):
        me = await self.tg.get_me()
        log.info("telegram bot @%s online", me.get("username"))
        try:
            await self.tg.call("deleteWebhook", {"drop_pending_updates": False})
        except TelegramError as e:
            log.warning("deleteWebhook: %s", e)
        offset = await self._skip_backlog()
        backoff = 2
        while not self._stop:
            try:
                updates = await self.tg.get_updates(offset)
                backoff = 2
            except TelegramError as e:
                if e.code == 409:
                    log.error("another program is polling this bot token (is the old elizaOS bot still running?)")
                    await asyncio.sleep(15)
                else:
                    log.warning("polling problem: %s", e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        await self.handle_message(u["message"])
                    except Exception:
                        log.exception("unhandled error on update %s", u.get("update_id"))
            if updates:
                await self.db.set_setting("telegram_offset", str(offset))
        for t in list(self._tasks):
            t.cancel()
