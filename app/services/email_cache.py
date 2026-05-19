"""File + in-memory hybrid cache for fetched Gmail messages.

Goals
-----
* Avoid re-fetching the same message repeatedly from Gmail (`messages.get` is
  the slowest call in the path).
* Provide page/limit pagination over a (sorted-by-date) inbox, so the frontend
  can scroll through 1 000+ messages without ever re-loading the entire inbox.
* Survive process restarts via a tiny JSON sidecar (`gmail_email_cache.json`).
* Stay correct under concurrent webhook + HTTP request access (single
  threading.Lock, atomic JSON write).

Per-message TTL is 60 s — after that, the next read will refresh metadata
through the incremental-sync layer, but message *bodies* are kept indefinitely
since they don't change.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from app.core.logging import get_logger

log = get_logger("email_cache")

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "gmail_email_cache.json")
_CACHE_PATH = os.path.abspath(_CACHE_PATH)

CACHE_TTL_SECONDS = 60

_lock = threading.Lock()
_memory: Dict[str, Dict[str, Any]] = {}
_loaded = False


def _disk_load() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(_CACHE_PATH):
        return {}
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        log.warning("Could not read email cache file: %s — starting empty", e)
        return {}


def _disk_save(data: Dict[str, Dict[str, Any]]) -> None:
    tmp = _CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _CACHE_PATH)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _memory.update(_disk_load())
        _loaded = True
        log.info("email cache loaded | %d messages", len(_memory))


def _key(email_address: str, message_id: str) -> str:
    return f"{email_address}::{message_id}"


def get_message(email_address: str, message_id: str) -> Optional[Dict[str, Any]]:
    """Return a cached message envelope or None if absent / fully expired."""
    _ensure_loaded()
    with _lock:
        entry = _memory.get(_key(email_address, message_id))
        if not entry:
            return None
        return entry.get("data")


def upsert_messages(email_address: str, messages: Iterable[Dict[str, Any]]) -> int:
    """Insert/replace messages in the cache. De-duplicates by Gmail message id."""
    _ensure_loaded()
    now = time.time()
    inserted = 0
    with _lock:
        for msg in messages:
            mid = msg.get("id")
            if not mid:
                continue
            k = _key(email_address, mid)
            _memory[k] = {"data": msg, "cached_at": now}
            inserted += 1
        _disk_save(_memory)
    if inserted:
        log.info("cache upsert | %s | %d messages", email_address, inserted)
    return inserted


def remove_messages(email_address: str, message_ids: Iterable[str]) -> int:
    _ensure_loaded()
    removed = 0
    with _lock:
        for mid in message_ids:
            k = _key(email_address, mid)
            if k in _memory:
                _memory.pop(k)
                removed += 1
        if removed:
            _disk_save(_memory)
    if removed:
        log.info("cache remove | %s | %d messages", email_address, removed)
    return removed


def known_message_ids(email_address: str) -> set:
    """Return the set of cached message IDs for an account (used for dedup)."""
    _ensure_loaded()
    prefix = f"{email_address}::"
    with _lock:
        return {k[len(prefix):] for k in _memory.keys() if k.startswith(prefix)}


def is_first_page_fresh(email_address: str) -> bool:
    """Cheap freshness check: if the most-recent message in the cache was
    written less than CACHE_TTL_SECONDS ago we consider page 1 fresh enough
    to serve directly without any Gmail API round-trip."""
    _ensure_loaded()
    prefix = f"{email_address}::"
    latest = 0.0
    with _lock:
        for k, v in _memory.items():
            if k.startswith(prefix):
                latest = max(latest, v.get("cached_at", 0))
    if latest == 0.0:
        return False
    return (time.time() - latest) < CACHE_TTL_SECONDS


def list_paginated(
    email_address: str,
    page: int = 1,
    limit: int = 50,
) -> Dict[str, Any]:
    """Return a paginated, date-desc slice of the user's cached inbox."""
    _ensure_loaded()
    prefix = f"{email_address}::"
    page = max(1, page)
    limit = max(1, min(500, limit))

    with _lock:
        entries = [v["data"] for k, v in _memory.items() if k.startswith(prefix)]

    def _sort_key(m: Dict[str, Any]) -> float:
        ts = m.get("internal_date") or 0
        try:
            return float(ts)
        except (TypeError, ValueError):
            return 0.0

    entries.sort(key=_sort_key, reverse=True)
    total = len(entries)
    start = (page - 1) * limit
    end = start + limit
    page_items = entries[start:end]
    has_more = end < total

    return {
        "emails": page_items,
        "page": page,
        "limit": limit,
        "total": total,
        "has_more": has_more,
    }


def list_all_messages(email_address: str) -> List[Dict[str, Any]]:
    """All cached envelopes for an account, newest first (for batch jobs)."""
    _ensure_loaded()
    prefix = f"{email_address}::"
    with _lock:
        entries = [v["data"] for k, v in _memory.items() if k.startswith(prefix)]

    def _sort_key(m: Dict[str, Any]) -> float:
        ts = m.get("internal_date") or 0
        try:
            return float(ts)
        except (TypeError, ValueError):
            return 0.0

    entries.sort(key=_sort_key, reverse=True)
    return entries


def clear(email_address: Optional[str] = None) -> None:
    _ensure_loaded()
    with _lock:
        if email_address is None:
            _memory.clear()
        else:
            prefix = f"{email_address}::"
            for k in [k for k in _memory.keys() if k.startswith(prefix)]:
                _memory.pop(k)
        _disk_save(_memory)
        log.info("cache cleared | scope=%s", email_address or "ALL")
