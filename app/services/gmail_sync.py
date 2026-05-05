"""High-level orchestration for Gmail push + incremental sync.

This module ties together:
  * `gmail_service`  - raw Gmail API helpers
  * `watch_state`    - persistent per-account watch / historyId store
  * `email_cache`    - deduplicated, file-backed message cache

Public entry points:
  * `register_watch_for_user(creds)`        - called by `POST /gmail/watch`
  * `unregister_watch_for_user(email)`      - called by `POST /gmail/stop`
  * `process_history_event(email, history)` - called by webhook handler
  * `incremental_sync(email)`               - manual / fallback trigger
  * `bootstrap_inbox(creds, email, limit)`  - one-time inbox seed when a user
                                              first turns on push notifications.
"""

from __future__ import annotations

import time
import threading
from typing import Any, Dict, List, Optional

from app.core import event_bus
from app.core.logger import get_logger
from app.services import email_cache, gmail_service, watch_state

log = get_logger("gmail_sync")

# How many messages to seed the cache with on first watch registration.
BOOTSTRAP_LIMIT = 100

# Max retry attempts for a single message fetch (handles transient Gmail errors).
_MESSAGE_FETCH_RETRIES = 3
_MESSAGE_FETCH_BACKOFF = 0.7  # seconds, exponential

_watch_init_lock = threading.Lock()
_watch_init_in_progress = set()


# ─── Internal helpers ────────────────────────────────────────────────────────

def _fetch_with_retry(creds: Dict[str, Any], message_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single message with simple exponential-backoff retries."""
    last_err: Optional[Exception] = None
    for attempt in range(1, _MESSAGE_FETCH_RETRIES + 1):
        try:
            msg = gmail_service.get_message_full(creds, message_id)
            return msg
        except Exception as e:
            last_err = e
            sleep_for = _MESSAGE_FETCH_BACKOFF * (2 ** (attempt - 1))
            log.warning(
                "messages.get failed for %s (attempt %d/%d): %s — backing off %.1fs",
                message_id, attempt, _MESSAGE_FETCH_RETRIES, e, sleep_for,
            )
            time.sleep(sleep_for)
    log.error("Giving up on message %s — last error: %s", message_id, last_err)
    return None


def _hydrate_messages(
    creds: Dict[str, Any], email_address: str, message_ids: List[str]
) -> List[Dict[str, Any]]:
    """Resolve a list of Gmail message IDs into full envelopes, skipping any
    we already have cached (dedup) and any that 404 (gone)."""
    if not message_ids:
        return []

    known = email_cache.known_message_ids(email_address)
    fresh_ids = [mid for mid in message_ids if mid not in known]
    skipped = len(message_ids) - len(fresh_ids)
    if skipped:
        log.info("dedup skipped %d already-cached messages", skipped)

    fetched: List[Dict[str, Any]] = []
    for mid in fresh_ids:
        msg = _fetch_with_retry(creds, mid)
        if msg:
            fetched.append(msg)
    if fetched:
        email_cache.upsert_messages(email_address, fetched)
    return fetched


# ─── Public entry points ─────────────────────────────────────────────────────

def register_watch_for_user(creds: Dict[str, Any], topic: Optional[str] = None) -> Dict[str, Any]:
    """Register a Gmail push watch and bootstrap the inbox cache.

    Returns a small descriptor the route layer can echo back to the client.
    """
    email_address = gmail_service.get_user_email(creds)
    if not email_address:
        raise RuntimeError("Could not determine user email address from Gmail profile")

    watch_resp = gmail_service.start_watch(creds, topic_name=topic)
    history_id = str(watch_resp.get("historyId", ""))
    expiration = watch_resp.get("expiration")
    topic_name = topic or gmail_service.GMAIL_PUBSUB_TOPIC

    watch_state.upsert_watch(
        email_address=email_address,
        history_id=history_id,
        watch_expiration=int(expiration) if expiration else None,
        topic_name=topic_name,
        credentials=creds,
    )

    # Seed the cache so /emails has something to serve immediately.
    seeded = bootstrap_inbox(creds, email_address, limit=BOOTSTRAP_LIMIT)

    log.info(
        "watch ready | %s | historyId=%s | seeded=%d", email_address, history_id, seeded
    )
    return {
        "email_address": email_address,
        "history_id": history_id,
        "watch_expiration": expiration,
        "topic_name": topic_name,
        "seeded_messages": seeded,
    }


def ensure_watch_for_user(creds: Dict[str, Any], topic: Optional[str] = None) -> Dict[str, Any]:
    """Idempotently create a Gmail watch for a user.

    This is safe to call from login, /emails lazy-init, or manual flows. It
    skips work if a watch already exists and prevents duplicate in-flight
    registrations for the same Gmail account.
    """
    email_address = gmail_service.get_user_email(creds)
    if not email_address:
        raise RuntimeError("Could not determine user email address from Gmail profile")

    existing = watch_state.get(email_address)
    if existing and not watch_state.is_expired(existing):
        log.info("watch already active for %s", email_address)
        return {
            "email_address": email_address,
            "history_id": existing.get("history_id"),
            "watch_expiration": existing.get("watch_expiration"),
            "topic_name": existing.get("topic_name"),
            "already_active": True,
        }

    with _watch_init_lock:
        if email_address in _watch_init_in_progress:
            log.info("watch initialization already running for %s", email_address)
            return {"email_address": email_address, "initializing": True}
        _watch_init_in_progress.add(email_address)

    try:
        return register_watch_for_user(creds, topic=topic)
    finally:
        with _watch_init_lock:
            _watch_init_in_progress.discard(email_address)


def unregister_watch_for_user(email_address: str) -> None:
    entry = watch_state.get(email_address)
    if not entry:
        log.info("unregister_watch: no entry for %s", email_address)
        return
    try:
        gmail_service.stop_watch(entry["credentials"])
    finally:
        watch_state.remove(email_address)


def renew_watch_if_needed(email_address: str) -> bool:
    """Re-register the watch if it's expired or about to expire.

    Returns True if a renewal was performed.
    """
    entry = watch_state.get(email_address)
    if not entry:
        return False
    if not watch_state.is_expired(entry):
        return False
    log.info("watch for %s is near expiration — renewing", email_address)
    try:
        register_watch_for_user(entry["credentials"], topic=entry.get("topic_name"))
        return True
    except Exception as e:
        log.error("watch renewal failed for %s: %s", email_address, e)
        return False


def bootstrap_inbox(
    creds: Dict[str, Any], email_address: str, limit: int = BOOTSTRAP_LIMIT
) -> int:
    """Pull the first `limit` INBOX messages once, so paginated reads work
    immediately after a user enables push notifications."""
    log.info("bootstrap inbox | %s | limit=%d", email_address, limit)
    result = gmail_service.fetch_emails(creds, force_refresh=True, limit=limit)
    msgs = result.get("emails", []) or []
    # `fetch_emails` returns mostly-shaped envelopes already.
    # Fill in `internal_date` if missing so cache sort works.
    for m in msgs:
        m.setdefault("internal_date", 0)
    email_cache.upsert_messages(email_address, msgs)
    if msgs:
        event_bus.notify(email_address, {"type": "inbox_updated", "added": len(msgs), "updated": 0, "removed": 0})
    return len(msgs)


def incremental_sync(email_address: str) -> Dict[str, Any]:
    """Apply Gmail history changes since our last stored historyId.

    Used by:
      * the webhook handler (after receiving a Pub/Sub push)
      * the periodic safety scheduler
      * the manual `POST /gmail/sync` endpoint
    """
    entry = watch_state.get(email_address)
    if not entry:
        raise RuntimeError(f"No watch state for {email_address}; call /gmail/watch first.")

    creds = entry["credentials"]
    last_history_id = entry.get("history_id")

    if not last_history_id:
        log.info("no historyId on file — performing full bootstrap")
        seeded = bootstrap_inbox(creds, email_address)
        # After bootstrap, we still need a current historyId; re-arm watch.
        renew_watch_if_needed(email_address)
        return {"bootstrapped": seeded, "added": 0, "removed": 0, "updated": 0}

    log.info("incremental sync | %s | from historyId=%s", email_address, last_history_id)
    history = gmail_service.list_history(creds, last_history_id)

    if history.get("expired"):
        log.warning("historyId expired for %s — full resync", email_address)
        seeded = bootstrap_inbox(creds, email_address)
        renew_watch_if_needed(email_address)
        return {"bootstrapped": seeded, "added": 0, "removed": 0, "updated": 0}

    added = _hydrate_messages(creds, email_address, history.get("added_ids", []))
    updated = _hydrate_messages(creds, email_address, history.get("updated_ids", []))

    removed_ids = history.get("removed_ids", [])
    if removed_ids:
        email_cache.remove_messages(email_address, removed_ids)

    new_history_id = history.get("latest_history_id")
    if new_history_id and new_history_id != last_history_id:
        watch_state.update_history_id(email_address, new_history_id)

    log.info(
        "incremental sync done | %s | +%d -%d ~%d | newHistoryId=%s",
        email_address, len(added), len(removed_ids), len(updated), new_history_id,
    )

    # Push a real-time signal to any open browser tab via SSE.
    if added or updated or removed_ids:
        event_bus.notify(
            email_address,
            {
                "type": "inbox_updated",
                "added": len(added),
                "updated": len(updated),
                "removed": len(removed_ids),
            },
        )

    return {
        "added": len(added),
        "removed": len(removed_ids),
        "updated": len(updated),
        "history_id": new_history_id,
    }


def process_history_event(email_address: str, push_history_id: Optional[str]) -> Dict[str, Any]:
    """Entry point used by the Pub/Sub webhook.

    The Pub/Sub payload carries the *current* historyId of the mailbox; we ignore
    its value for incremental fetching (we always sync forward from our own
    last-stored historyId) but log it for traceability.
    """
    log.info("webhook trigger | %s | push_history_id=%s", email_address, push_history_id)
    return incremental_sync(email_address)
