"""Persistent watch-state store.

For each Gmail account we track:

* `credentials`        - OAuth credentials (so the webhook handler, which has no
                         HTTP session context, can still call the Gmail API).
* `history_id`         - last `historyId` synced (incremental sync anchor).
* `watch_expiration`   - epoch ms when Gmail's `users.watch` registration
                         expires (Gmail watch lasts ~7 days, must be renewed).
* `topic_name`         - Pub/Sub topic the watch was registered against.
* `email_address`      - Gmail address the watch belongs to.
* `last_synced_at`     - UNIX timestamp of last successful incremental sync.

The store is a small JSON file on disk keyed by email address. JSON is fine for
the local-dev / single-tenant use case described in the task and avoids a
database dependency. All access is guarded by a process-wide lock.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Iterable, Optional

from app.core.logger import get_logger

log = get_logger("watch_state")

_STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "gmail_watch_state.json")
_STATE_PATH = os.path.abspath(_STATE_PATH)
_lock = threading.Lock()


def _load() -> Dict[str, Any]:
    if not os.path.exists(_STATE_PATH):
        return {}
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        log.warning("Failed to read watch state file: %s — starting empty", e)
        return {}


def _save(data: Dict[str, Any]) -> None:
    tmp_path = _STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, _STATE_PATH)


def upsert_watch(
    email_address: str,
    history_id: str,
    watch_expiration: Optional[int],
    topic_name: str,
    credentials: Dict[str, Any],
) -> None:
    """Save or update the watch entry for an account."""
    with _lock:
        data = _load()
        existing = data.get(email_address, {})
        existing.update(
            {
                "email_address": email_address,
                "history_id": str(history_id),
                "watch_expiration": int(watch_expiration) if watch_expiration else None,
                "topic_name": topic_name,
                "credentials": credentials,
                "last_synced_at": time.time(),
            }
        )
        data[email_address] = existing
        _save(data)
        log.info(
            "watch upserted for %s | historyId=%s | expires=%s",
            email_address,
            history_id,
            existing.get("watch_expiration"),
        )


def update_history_id(email_address: str, history_id: str) -> None:
    """Persist the latest historyId after a successful incremental sync."""
    with _lock:
        data = _load()
        if email_address not in data:
            log.debug("update_history_id: no entry for %s", email_address)
            return
        data[email_address]["history_id"] = str(history_id)
        data[email_address]["last_synced_at"] = time.time()
        _save(data)
        log.info("historyId for %s -> %s", email_address, history_id)


def get(email_address: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _load().get(email_address)


def all_watches() -> Iterable[Dict[str, Any]]:
    with _lock:
        return list(_load().values())


def remove(email_address: str) -> None:
    with _lock:
        data = _load()
        if email_address in data:
            data.pop(email_address)
            _save(data)
            log.info("watch removed for %s", email_address)


def is_expired(entry: Dict[str, Any], skew_seconds: int = 3600) -> bool:
    """A watch is considered expired (or about-to-expire) `skew_seconds`
    before its real expiration so we can renew proactively.
    Gmail returns `expiration` in ms since epoch."""
    exp_ms = entry.get("watch_expiration")
    if not exp_ms:
        return True
    return (exp_ms / 1000.0) - skew_seconds <= time.time()
