import asyncio
import queue
import threading


class InterruptBus:
    """Lets external callers inject user messages into a running agent's loop.

    The agent registers its own queue at run-start and drains it between
    API calls.  The server calls send() from any thread.
    """

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._queues: dict[str, queue.Queue] = {}

    def register(self, key: str, q: "queue.Queue[str]") -> None:
        with self._lock:
            self._queues[key] = q

    def unregister(self, key: str) -> None:
        with self._lock:
            self._queues.pop(key, None)

    def send(self, key: str, message: str) -> bool:
        """Return True if the agent is currently running and the message was queued."""
        with self._lock:
            q = self._queues.get(key)
        if q is None:
            return False
        q.put_nowait(message)
        return True

    def active(self) -> list[str]:
        with self._lock:
            return list(self._queues.keys())


interrupt_bus = InterruptBus()


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
