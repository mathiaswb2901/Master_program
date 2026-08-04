"""In-process pub/sub bus. WebSocket handlers subscribe; services publish."""

import asyncio

from pydantic import BaseModel

_QUEUE_LIMIT = 1000  # a subscriber this far behind is dead weight; drop oldest


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[BaseModel]] = set()

    def subscribe(self) -> asyncio.Queue[BaseModel]:
        q: asyncio.Queue[BaseModel] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[BaseModel]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: BaseModel) -> None:
        for q in self._subscribers:
            if q.full():
                q.get_nowait()  # drop oldest rather than block the publisher
            q.put_nowait(event)
