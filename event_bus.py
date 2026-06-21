import asyncio
import queue
import threading


class InterruptBus:
    """Per-agent persistent inbox for mid-run (or pre-run) user messages.

    Messages sent to an agent that isn't running yet sit in the inbox and
    are drained at the top of every agentic-loop step once the agent starts.
    Sending to any agent always succeeds — the message is never dropped.
    """

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._inbox:  dict[str, queue.Queue] = {}   # persists across runs
        self._active: set[str]               = set()

    def register(self, key: str) -> "queue.Queue[str]":
        """Called by the agent at run-start. Returns (possibly pre-filled) inbox."""
        with self._lock:
            if key not in self._inbox:
                self._inbox[key] = queue.Queue()
            self._active.add(key)
            return self._inbox[key]

    def unregister(self, key: str) -> None:
        """Called at run-end. Inbox is kept so pending messages survive."""
        with self._lock:
            self._active.discard(key)

    def send(self, key: str, message: str) -> dict:
        """Queue a message for any agent, running or not."""
        with self._lock:
            if key not in self._inbox:
                self._inbox[key] = queue.Queue()
            self._inbox[key].put_nowait(message)
            live = key in self._active
        return {"queued": True, "live": live}

    def active(self) -> list[str]:
        with self._lock:
            return list(self._active)


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
