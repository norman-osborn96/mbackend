"""In-process, thread-safe event broadcaster.

When `gmail_sync.incremental_sync` finds new or changed emails it calls
`notify(email_address, payload)`. Any SSE connection open for that user
receives the payload immediately, triggering an inbox refresh in the browser
without polling.

Design
------
* One `queue.Queue` per active SSE connection (supports multiple tabs).
* Plain `threading.Lock` — safe from both sync background tasks and the
  uvicorn async thread.
* No external dependencies (no Redis, no Celery).
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List

_lock = threading.Lock()
_subscribers: Dict[str, List[queue.Queue]] = {}


def subscribe(email_address: str) -> queue.Queue:
    """Return a new queue that will receive events for this user."""
    q: queue.Queue = queue.Queue()
    with _lock:
        _subscribers.setdefault(email_address, []).append(q)
    return q


def unsubscribe(email_address: str, q: queue.Queue) -> None:
    with _lock:
        subs = _subscribers.get(email_address, [])
        try:
            subs.remove(q)
        except ValueError:
            pass


def notify(email_address: str, data: Any) -> None:
    """Push `data` to every active SSE connection for this user."""
    with _lock:
        queues = list(_subscribers.get(email_address, []))
    for q in queues:
        try:
            q.put_nowait(data)
        except Exception:
            pass


def active_connections(email_address: str) -> int:
    with _lock:
        return len(_subscribers.get(email_address, []))
