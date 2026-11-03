import asyncio
import json

from astrid.db import Database
from astrid.engine import Astrid
from astrid.telegram_bot import Bot, Telegram
from conftest import FakeLLM, make_cfg

APPRAISAL = json.dumps({"deltas": {"rapport": 0.5}, "mood": "warm", "directive": "keep it light", "note": "nice"})


def msg(uid, text, chat_type="private", chat_id=None):
    return {"message_id": 1, "from": {"id": uid}, "chat": {"id": chat_id or uid, "type": chat_type}, "text": text}


class FakeTelegramAPI:
    def __init__(self, server, backlog, queue):
        self.sent, self.actions, self.deleted_webhook = [], [], False
        self.backlog, self.queue, self.fail_updates = backlog, queue, []
        server.handler = self.handle

    def handle(self, path, body):
        method = path.rsplit("/", 1)[1]
        if method == "getMe":
            return 200, {"ok": True, "result": {"username": "astrid_bot"}}
        if method == "deleteWebhook":
            self.deleted_webhook = True
            return 200, {"ok": True, "result": True}
        if method == "sendChatAction":
            self.actions.append(body)
            return 200, {"ok": True, "result": True}
        if method == "sendMessage":
            self.sent.append(body)
            return 200, {"ok": True, "result": {}}
        if method == "getUpdates":
            if self.fail_updates:
                return self.fail_updates.pop(0)
            if body.get("offset") == -1:
                return 200, {"ok": True, "result": self.backlog}
            out, self.queue = self.queue, []
            if not out:
                import time; time.sleep(0.05)
            return 200, {"ok": True, "result": out}
        return 404, {"ok": False, "description": "nope"}


async def drive(db_url, server, api, llm=None, cfg_over=None, expect_sent=1):
    cfg = make_cfg(db_url, telegram_api_base=server.url, **(cfg_over or {}))
    db = Database(db_url)
    await db.migrate()
    llm = llm or FakeLLM()
    if not llm.scripts:
        llm.script("euryale", "hello there")
        llm.script("r1", APPRAISAL)
    bot = Bot(cfg, db, Astrid(cfg, db, llm), Telegram("T", server.url))
    task = asyncio.create_task(bot.run())
    for _ in range(100):
        if len(api.sent) >= expect_sent:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.4)   # let background appraisal finish
    bot.stop()
    await asyncio.wait_for(task, 5)
    return db, llm


def test_full_flow_backlog_owner_and_filtering(db_url, server):
    api = FakeTelegramAPI(
        server,
        backlog=[{"update_id": 5, "message": msg(42, "old message from yesterday")}],
        queue=[
            {"update_id": 6, "message": msg(42, "hi astrid")},
            {"update_id": 7, "message": msg(99, "stranger here")},
            {"update_id": 8, "message": msg(42, "/start")},
            {"update_id": 9, "message": msg(42, "group msg", chat_type="group", chat_id=-100)},
            {"update_id": 10, "message": {"message_id": 2, "from": {"id": 42}, "chat": {"id": 42, "type": "private"}}},  # no text
        ],
    )
    async def go():
        db, llm = await drive(db_url, server, api)
        owner = await db.get_setting("owner_telegram_id")
        offset = await db.get_setting("telegram_offset")
        st = await db.get_state()
        msgs = await db.recent_messages(42, 10)
        await db.close()
        return owner, offset, st, msgs, llm
    owner, offset, st, msgs, llm = asyncio.run(go())
    assert api.deleted_webhook
    assert api.sent == [{"chat_id": 42, "text": "hello there"}]       # one reply: backlog, stranger, command, group, no-text all ignored
    assert owner == "42" and offset == "11"
    assert [m.content for m in msgs] == ["hi astrid", "hello there"]
    assert api.actions and api.actions[0]["action"] == "typing"
    assert st.mood == "warm" and st.directive == "keep it light"     # background appraisal ran after the reply
    assert all("old message" not in json.dumps(c["messages"]) for c in llm.calls)


def test_configured_owner_beats_first_come(db_url, server):
    api = FakeTelegramAPI(server, backlog=[], queue=[
        {"update_id": 1, "message": msg(99, "i am not the owner")},
        {"update_id": 2, "message": msg(42, "i am")},
    ])
    async def go():
        db, _ = await drive(db_url, server, api, cfg_over={"owner_telegram_id": 42})
        stored = await db.get_setting("owner_telegram_id")
        await db.close()
        return stored
    assert asyncio.run(go()) is None
    assert [s["chat_id"] for s in api.sent] == [42]


def test_reply_failure_sends_glitch_note_and_bot_survives(db_url, server):
    api = FakeTelegramAPI(server, backlog=[], queue=[
        {"update_id": 1, "message": msg(42, "first")},
        {"update_id": 2, "message": msg(42, "second")},
    ])
    llm = FakeLLM()
    llm.script("euryale", RuntimeError("provider down"), "recovered")
    llm.script("r1", APPRAISAL)
    asyncio.run(drive(db_url, server, api, llm=llm, expect_sent=2))
    assert [s["text"] for s in api.sent] == ["(i glitched, say that again)", "recovered"]


def test_polling_errors_back_off_and_recover(db_url, server, monkeypatch):
    import astrid.telegram_bot as tb
    real_sleep = asyncio.sleep
    async def fast(s):
        await real_sleep(0.01)
    monkeypatch.setattr(tb.asyncio, "sleep", fast)
    api = FakeTelegramAPI(server, backlog=[], queue=[{"update_id": 1, "message": msg(42, "hi")}])
    api.fail_updates = [
        (502, {"ok": False, "description": "bad gateway"}),
        (409, {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}),
    ]
    asyncio.run(drive(db_url, server, api))
    assert [s["text"] for s in api.sent] == ["hello there"]


def test_restart_does_not_replay_old_updates(db_url, server):
    api = FakeTelegramAPI(server, backlog=[], queue=[{"update_id": 3, "message": msg(42, "hi")}])
    async def go():
        db, _ = await drive(db_url, server, api)
        await db.close()
        # second run: server still has an old backlog; stored offset must win and nothing is re-answered
        api.backlog = [{"update_id": 3, "message": msg(42, "hi")}]
        api.sent.clear()
        db2, _ = await drive(db_url, server, api, expect_sent=0)
        off = await db2.get_setting("telegram_offset")
        await db2.close()
        return off
    off = asyncio.run(go())
    assert api.sent == [] and off == "4"


def test_meh_flags_last_reply_with_note_and_never_reaches_her(db_url, server):
    api = FakeTelegramAPI(server, backlog=[], queue=[
        {"update_id": 1, "message": msg(42, "what have you been up to")},
        {"update_id": 2, "message": msg(42, "/meh too philosophical")},
        {"update_id": 3, "message": msg(99, "/meh@astrid_bot stranger")},
        {"update_id": 4, "message": msg(42, "/MEH@astrid_bot")},
    ])
    async def go():
        db, llm = await drive(db_url, server, api, expect_sent=3)
        flags = await db.list_flags()
        msgs = await db.recent_messages(42, 10)
        await db.close()
        return flags, msgs, llm
    flags, msgs, llm = asyncio.run(go())
    assert [s["text"] for s in api.sent] == ["hello there", "(noted)", "(noted)"]
    assert [(f["note"], f["reply"], f["user_text"]) for f in reversed(flags)] == [
        ("too philosophical", "hello there", "what have you been up to"),
        (None, "hello there", "what have you been up to"),
    ]
    assert [m.content for m in msgs] == ["what have you been up to", "hello there"]   # commands are not conversation
    assert len(llm.calls) == 2                                                          # one reply, one appraisal


def test_meh_with_nothing_to_flag(db_url, server):
    api = FakeTelegramAPI(server, backlog=[], queue=[{"update_id": 1, "message": msg(42, "/meh")}])
    async def go():
        db = Database(db_url)
        await db.migrate()
        await db.set_setting("owner_telegram_id", "42")
        await db.close()
        db, _ = await drive(db_url, server, api)
        flags = await db.list_flags()
        await db.close()
        return flags
    assert asyncio.run(go()) == []
    assert [s["text"] for s in api.sent] == ["(nothing to flag yet)"]
