import asyncio
import threading


class EventBus:
    """Bridge between agent worker threads and async SSE subscribers.

    Agents call emit() from any thread.
    SSE endpoint calls subscribe() / unsubscribe() from the asyncio event loop.
    """

    def __init__(self) -> None:
        self._lock        = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not q]

    def emit(self, event: dict) -> None:
        if not self._loop:
            return
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            self._loop.call_soon_threadsafe(q.put_nowait, event)


bus = EventBus()
