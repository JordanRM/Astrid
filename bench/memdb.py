"""In-memory stand-in for the database, so bench runs never touch her real conversation or mood."""
from __future__ import annotations

from astrid.db import Message
from astrid.state import State


class MemoryDB:
    def __init__(self, state: State):
        self.state = state
        self.messages: list[Message] = []

    async def add_message(self, chat_id, role, content, ts, channel="telegram"):
        self.messages.append(Message(role, content, ts))

    async def recent_messages(self, chat_id, limit):
        return self.messages[-limit:]

    async def get_state(self) -> State:
        s = self.state
        return State(dict(s.values), s.mood, s.unresolved_tension, s.directive, s.updated_at)
