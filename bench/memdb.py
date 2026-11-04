"""In-memory stand-in for the database, so bench runs never touch her real conversation or mood."""
from __future__ import annotations

from astrid.db import Message
from astrid.state import State


class MemoryDB:
    def __init__(self, state: State, opinions: list | None = None, thoughts: list | None = None):
        self.state = state
        self.thoughts = list(thoughts or [])
        self.opinions = list(opinions or [])
        self.messages: list[Message] = []

    async def add_message(self, chat_id, role, content, ts, channel="telegram"):
        self.messages.append(Message(role, content, ts))

    async def recent_messages(self, chat_id, limit):
        return self.messages[-limit:]

    async def get_state(self) -> State:
        s = self.state
        return State(dict(s.values), s.mood, s.unresolved_tension, s.directive, s.updated_at)

    async def list_opinions(self, limit=60):
        return self.opinions[:limit]

    async def list_thoughts(self, limit=30):
        return self.thoughts[:limit]

    async def mark_thoughts_surfaced(self, ids):
        for t in self.thoughts:
            if t.get("id") in ids:
                t["surfaced"] = (t.get("surfaced") or 0) + 1
