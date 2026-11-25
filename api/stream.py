"""Server-sent events, one-time stream tickets and the worker's wake-up hints.

All in memory (one API instance). No database connection is held for a stream
(contracts decision 10): the worker pushes events over ``POST /internal/events``
and the API fans them out. Each workspace keeps its last 500 events so a
reconnect with ``Last-Event-ID`` can resume; ids look like ``<boot>-<n>`` so an
id from before an API restart is recognised and answered with ``resync``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from uuid import UUID

from api.core import crypto

BUFFER = 500
TICKET_TTL = 60.0


@dataclass(frozen=True)
class Message:
    id: str
    event: str
    data: dict

    def encode(self) -> str:
        return f"id: {self.id}\nevent: {self.event}\ndata: {json.dumps(self.data, separators=(',', ':'))}\n\n"


class Hub:
    def __init__(self) -> None:
        self.boot = str(int(time.time() * 1000))
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}
        self._buffers: dict[str, deque[Message]] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, workspace_id: UUID | str, event: str, data: dict) -> Message:
        ws = str(workspace_id)
        with self._lock:
            n = self._seq.get(ws, 0) + 1
            self._seq[ws] = n
            msg = Message(f"{self.boot}-{n}", event, data)
            self._buffers.setdefault(ws, deque(maxlen=BUFFER)).append(msg)
            subs = list(self._subs.get(ws, ()))
        for q in subs:
            self._deliver(q, msg)
        return msg

    def _deliver(self, q: asyncio.Queue, msg: Message) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _put(q, msg)
        else:
            loop.call_soon_threadsafe(_put, q, msg)

    def subscribe(self, workspace_id: UUID | str, last_event_id: str | None) -> tuple[asyncio.Queue, list[Message]]:
        """Register a subscriber; return its queue and what to replay first."""
        ws = str(workspace_id)
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.setdefault(ws, set()).add(q)
            buf = list(self._buffers.get(ws, ()))
        replay: list[Message] = []
        if last_event_id:
            boot, _, num = last_event_id.partition("-")
            ids = [m.id for m in buf]
            if boot != self.boot or not num.isdigit():
                replay = [Message(f"{self.boot}-0", "resync", {})]
            elif last_event_id in ids:
                replay = buf[ids.index(last_event_id) + 1 :]
            elif buf and int(num) < int(buf[0].id.split("-")[1]):
                replay = [Message(f"{self.boot}-0", "resync", {})]  # fell out of the buffer
        return q, replay

    def unsubscribe(self, workspace_id: UUID | str, q: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subs.get(str(workspace_id))
            if subs:
                subs.discard(q)

    def subscriber_count(self, workspace_id: UUID | str) -> int:
        with self._lock:
            return len(self._subs.get(str(workspace_id), ()))


def _put(q: asyncio.Queue, msg: Message) -> None:
    try:
        q.put_nowait(msg)
    except asyncio.QueueFull:
        pass  # a stalled client misses events; it will resync on reconnect


@dataclass(frozen=True)
class Ticket:
    user_id: UUID
    workspace_id: UUID
    expires_at: float


class Tickets:
    """One-time tickets for ``GET /v1/stream``: single use, 60 s, bound to user and workspace."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[bytes, Ticket] = {}

    def issue(self, user_id: UUID, workspace_id: UUID) -> tuple[str, float]:
        token = crypto.random_token(32)
        expires = time.time() + TICKET_TTL
        with self._lock:
            now = time.time()
            for k in [k for k, t in self._tickets.items() if t.expires_at < now]:
                del self._tickets[k]
            self._tickets[crypto.sha256(token)] = Ticket(user_id, workspace_id, expires)
        return token, expires

    def redeem(self, token: str) -> Ticket | None:
        with self._lock:
            t = self._tickets.pop(crypto.sha256(token), None)
        if t is None or t.expires_at < time.time():
            return None
        return t


class WorkHints:
    """Wake-up hints for the worker, which long-polls ``GET /internal/work``.

    Hints only make the worker look sooner; the durable state is in the
    database (webhook_events rows, sources.last_change_at), so a hint lost to an
    API restart is picked up on the worker's next scheduled pass.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hints: dict[str, set[str]] = {}
        self._event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._event = asyncio.Event()

    def add(self, workspace_id: UUID | str, reason: str) -> None:
        with self._lock:
            self._hints.setdefault(str(workspace_id), set()).add(reason)
            if len(self._hints) > 10_000:
                self._hints.pop(next(iter(self._hints)))
        if self._loop is not None and self._event is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._event.set)

    def take(self) -> dict[str, list[str]]:
        with self._lock:
            out = {k: sorted(v) for k, v in self._hints.items()}
            self._hints.clear()
        if self._event is not None:
            self._event.clear()
        return out

    async def wait(self, timeout: float) -> dict[str, list[str]]:
        with self._lock:
            pending = bool(self._hints)
        if not pending and self._event is not None:
            try:
                await asyncio.wait_for(self._event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return self.take()
