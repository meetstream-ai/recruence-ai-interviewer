"""Per-session event bus feeding the dashboard over server-sent events."""

from __future__ import annotations

import asyncio


class EventBus:
    """Keeps the session's history so a reconnecting browser can replay it."""

    def __init__(self) -> None:
        self.history: list[dict] = []
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, event: dict) -> dict:
        event = {"seq": len(self.history) + 1, **event}
        self.history.append(event)
        for q in self._subscribers:
            q.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for event in self.history:
            q.put_nowait(event)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
