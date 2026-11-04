"""Postgres access. One long-lived connection with reconnect, safe for Supabase poolers."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .state import BASELINE, FIELDS, State

log = logging.getLogger("astrid.db")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


@dataclass
class Message:
    role: str
    content: str
    ts: float


class Database:
    def __init__(self, url: str):
        self._url = url
        self._conn: psycopg.AsyncConnection | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> psycopg.AsyncConnection:
        # prepare_threshold=None: no server-side prepared statements, so poolers never choke.
        return await psycopg.AsyncConnection.connect(
            self._url, autocommit=True, row_factory=dict_row, prepare_threshold=None, connect_timeout=15,
            client_encoding="UTF8"
        )

    async def _run(self, op):
        """Run op(conn); reconnect and retry once if the connection died."""
        async with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = await self._connect()
                    return await op(self._conn)
                except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                    log.warning("database connection problem (%s); attempt %d", e, attempt)
                    try:
                        if self._conn is not None:
                            await self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                    if attempt == 2:
                        raise

    async def close(self):
        async with self._lock:
            if self._conn is not None and not self._conn.closed:
                await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params=None) -> None:
        async def op(c):
            await c.execute(sql, params)
        await self._run(op)

    async def fetch(self, sql: str, params=None) -> list[dict]:
        async def op(c):
            cur = await c.execute(sql, params)
            return await cur.fetchall()
        return await self._run(op)

    async def fetchrow(self, sql: str, params=None) -> dict | None:
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    # ---- migrations ----
    async def migrate(self) -> list[str]:
        await self.execute("create schema if not exists astrid")
        await self.execute(
            "create table if not exists astrid.schema_migrations "
            "(name text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["name"] for r in await self.fetch("select name from astrid.schema_migrations")}
        applied = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            sql = path.read_text()

            async def op(c, sql=sql, name=path.name):
                async with c.transaction():
                    await c.execute(sql)
                    await c.execute("insert into astrid.schema_migrations (name) values (%s)", (name,))

            await self._run(op)
            applied.append(path.name)
            log.info("applied migration %s", path.name)
        return applied

    # ---- settings ----
    async def get_setting(self, key: str) -> str | None:
        row = await self.fetchrow("select value from astrid.settings where key = %s", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "insert into astrid.settings (key, value) values (%s, %s) "
            "on conflict (key) do update set value = excluded.value",
            (key, value),
        )

    # ---- messages ----
    async def add_message(self, chat_id: int, role: str, content: str, ts: float, channel: str = "telegram") -> None:
        await self.execute(
            "insert into astrid.messages (chat_id, role, content, channel, created_at) "
            "values (%s, %s, %s, %s, to_timestamp(%s))",
            (chat_id, role, content, channel, ts),
        )

    async def recent_messages(self, chat_id: int, limit: int) -> list[Message]:
        rows = await self.fetch(
            "select role, content, ts from ("
            "  select id, role, content, extract(epoch from created_at)::float8 as ts "
            "  from astrid.messages where chat_id = %s order by id desc limit %s"
            ") t order by id asc",
            (chat_id, limit),
        )
        return [Message(r["role"], r["content"], r["ts"]) for r in rows]

    # ---- flagged replies (/meh) ----
    async def last_exchange(self, chat_id: int) -> tuple[str | None, str] | None:
        """Her latest reply and the message from Jordan right before it. None if she hasn't replied yet."""
        msgs = await self.recent_messages(chat_id, 6)
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].role == "assistant":
                user = next((m.content for m in reversed(msgs[:i]) if m.role == "user"), None)
                return user, msgs[i].content
        return None

    async def add_flag(self, chat_id: int, user_text: str | None, reply: str, note: str | None,
                       mood: str | None, state_values: dict) -> None:
        await self.execute(
            "insert into astrid.flags (chat_id, user_text, reply, note, mood, state) values (%s, %s, %s, %s, %s, %s::jsonb)",
            (chat_id, user_text, reply, note, mood, json.dumps(state_values)),
        )

    async def list_flags(self, limit: int = 200) -> list[dict]:
        return await self.fetch(
            "select id, created_at, user_text, reply, note, mood from astrid.flags order by id desc limit %s", (limit,)
        )

    # ---- opinions ----
    async def list_opinions(self, limit: int = 60) -> list[dict]:
        return await self.fetch(
            "select topic, view, reason, source, confidence, history, extract(epoch from updated_at)::float8 as updated_at "
            "from astrid.opinions order by updated_at desc limit %s", (limit,),
        )

    async def upsert_opinion(self, topic: str, view: str, reason: str | None, source: str, confidence: float,
                             note: str | None, now: float) -> str:
        """Save a view. Returns 'formed', 'changed' (the view itself moved, old one goes to history) or 'kept'."""
        key = " ".join(topic.lower().split())[:80]

        async def op(c):
            async with c.transaction():
                cur = await c.execute("select view, confidence, history from astrid.opinions where topic = %s for update", (key,))
                row = await cur.fetchone()
                if row is None:
                    await c.execute(
                        "insert into astrid.opinions (topic, view, reason, source, confidence) values (%s, %s, %s, %s, %s)",
                        (key, view, reason, source, confidence),
                    )
                    return "formed"
                changed = " ".join(row["view"].lower().split()) != " ".join(view.lower().split())
                history = list(row["history"] or [])
                if changed:
                    history.append({"at": now, "view": row["view"], "confidence": row["confidence"], "note": note or ""})
                await c.execute(
                    "update astrid.opinions set view = %s, reason = %s, source = %s, confidence = %s, history = %s::jsonb, "
                    "updated_at = to_timestamp(%s) where topic = %s",
                    (view, reason, source, confidence, json.dumps(history), now, key),
                )
                return "changed" if changed else "kept"

        return await self._run(op)

    # ---- thoughts (her own reading) ----
    async def add_thought(self, t: dict, now: float) -> None:
        await self.execute(
            "insert into astrid.thoughts (created_at, kind, question, query, sources, thought, notes, learned, for_jordan, "
            "next_question, share, explicit) values (to_timestamp(%s), %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)",
            (now, t["kind"], t["question"], t.get("query"), json.dumps(t.get("sources") or []), t["thought"],
             t.get("notes"), t.get("learned"), t.get("for_jordan"), t.get("next_question"), t.get("share", 0.3), bool(t.get("explicit"))),
        )

    async def list_thoughts(self, limit: int = 30) -> list[dict]:
        return await self.fetch(
            "select id, extract(epoch from created_at)::float8 as created_at, kind, question, query, sources, thought, "
            "notes, learned, for_jordan, next_question, share, explicit, surfaced, followed "
            "from astrid.thoughts order by created_at desc limit %s", (limit,),
        )

    async def mark_thoughts_surfaced(self, ids: list[int]) -> None:
        if ids:
            await self.execute("update astrid.thoughts set surfaced = surfaced + 1 where id = any(%s)", (list(ids),))

    async def mark_thought_followed(self, thought_id: int) -> None:
        await self.execute("update astrid.thoughts set followed = true where id = %s", (thought_id,))

    async def recent_user_lines(self, limit: int = 60) -> list[str]:
        rows = await self.fetch(
            "select content from (select id, content from astrid.messages where role = 'user' order by id desc limit %s) t "
            "order by id asc", (limit,),
        )
        return [r["content"] for r in rows]

    async def last_message_at(self) -> float | None:
        row = await self.fetchrow("select extract(epoch from max(created_at))::float8 as at from astrid.messages")
        return row["at"] if row else None

    # ---- state ----
    async def get_state(self) -> State:
        row = await self.fetchrow(
            "select " + ", ".join(FIELDS) + ", mood, unresolved_tension, directive, "
            "extract(epoch from updated_at)::float8 as updated_at from astrid.state where id = 1"
        )
        if row is None:  # should not happen; migration inserts it
            return State()
        return State(
            values={k: float(row[k]) for k in FIELDS},
            mood=row["mood"],
            unresolved_tension=row["unresolved_tension"],
            directive=row["directive"],
            updated_at=row["updated_at"],
        )

    async def save_state(self, state: State, now: float, cause: str, deltas: dict, note: str | None) -> None:
        async def op(c):
            async with c.transaction():
                sets = ", ".join(f"{k} = %s" for k in FIELDS)
                await c.execute(
                    f"update astrid.state set {sets}, mood = %s, unresolved_tension = %s, directive = %s, "
                    "updated_at = to_timestamp(%s) where id = 1",
                    [state.values[k] for k in FIELDS]
                    + [state.mood, state.unresolved_tension, state.directive, now],
                )
                await c.execute(
                    "insert into astrid.state_history (at, cause, deltas, state, mood, note) "
                    "values (to_timestamp(%s), %s, %s::jsonb, %s::jsonb, %s, %s)",
                    (now, cause, json.dumps(deltas), json.dumps(state.values), state.mood, note),
                )

        await self._run(op)

    # ---- reset ----
    async def reset_all(self, now: float) -> None:
        """Forget the conversation and put her back to a first-meeting state. Keeps settings."""
        await self.execute("truncate astrid.messages, astrid.state_history, astrid.opinions, astrid.thoughts restart identity")
        fresh = State(dict(BASELINE), "neutral", None, None, now)
        await self.save_state(fresh, now, "reset", {}, "fresh start")
        await self.execute("delete from astrid.settings where key = 'wander_next'")
