"""An append-only log of things that happened to the runtime.

The control interface and any client app can poll /control/state for a
snapshot of *now*, but a snapshot cannot answer "what changed while I was not
looking". A phone that was in a pocket, or a wall display that was asleep,
needs the transitions rather than the current frame. This is that list.

Deliberately in memory and bounded. Events are operational history, not
records: the alternative is a disk write on the request path plus rotation,
corruption handling and permissions, for data whose value expires in minutes.
The cost is that a gateway restart starts a fresh log, so every response
carries an `epoch` that changes when the process does — a client seeing a new
epoch knows to reset rather than to splice a new list onto an old one.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any


# Roughly a day of ordinary activity. Loading a model is a rare event; this is
# sized so a client polling once a minute cannot outrun it in practice.
DEFAULT_CAPACITY = 500
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# Two or more path segments, so "/home/you/models/x.gguf" is redacted while
# "GPU 0/1" and a bare "/" are left alone.
ABSOLUTE_PATH = re.compile(r"(?:/[^\s/]+){2,}/?")


def redact_paths(text: str) -> str:
    """Strip absolute paths out of anything destined for the feed.

    Failure summaries are built from upstream exception text, and those
    routinely name the executable or the weights file. The feed is readable
    with a display token, which is meant to see no filesystem layout at all,
    so redaction happens once at the point of record rather than at each of
    the call sites that could forget. The unredacted error is still available
    to an admin through /control/state.
    """
    return ABSOLUTE_PATH.sub("<path>", text)


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    at: float
    kind: str
    summary: str
    model: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventLog:
    """Bounded, monotonically numbered, safe to write from any thread.

    FastAPI runs synchronous endpoints in a threadpool and the control plane
    writes from the event loop, so every mutation takes the lock.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._events: deque[Event] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_id = 1
        # Identifies this process, not this log. A client compares it to detect
        # a restart, so it must not change while the process lives.
        self.epoch = secrets.token_hex(8)

    def record(self, kind: str, summary: str, model: str | None = None) -> Event:
        with self._lock:
            event = Event(
                id=self._next_id,
                at=time.time(),
                kind=kind,
                summary=redact_paths(summary),
                model=model,
            )
            self._next_id += 1
            self._events.append(event)
            return event

    def since(self, cursor: int | None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Events newer than `cursor`, oldest first.

        A client with no cursor is starting cold and gets the most recent
        `limit` events, so a fresh app opens onto history rather than a blank
        list. `truncated` says the cursor was older than anything still
        retained: the client missed events and should not assume continuity.
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        with self._lock:
            events = list(self._events)
            latest = self._next_id - 1

        if cursor is None:
            selected = events[-limit:]
            truncated = False
        else:
            selected = [event for event in events if event.id > cursor][:limit]
            # The oldest retained id is above the cursor, so whatever sat
            # between them has already been evicted.
            oldest = events[0].id if events else latest + 1
            truncated = bool(events) and cursor < oldest - 1

        return {
            "epoch": self.epoch,
            "cursor": selected[-1].id if selected else (cursor if cursor is not None else latest),
            "latest": latest,
            "truncated": truncated,
            "events": [event.public_dict() for event in selected],
        }
