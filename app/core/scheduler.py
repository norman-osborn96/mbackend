"""Background safety scheduler.

If the Pub/Sub webhook is misconfigured, the network swallows a notification,
or Gmail's watch expires before we renew it, we still want the inbox cache
to converge. This module spins up a small daemon thread that, every
`mailpulse_fallback_sync_seconds` from settings, walks all known watch entries and:

  1. Renews any watch that's expired or near expiration (Gmail watch ~7 days).
  2. Runs an incremental sync (history.list) for each known mailbox.

This is *not* the 1-minute polling we removed — it is a much-rarer safety net.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from app.core.logging import get_logger
from app.services import gmail_sync, watch_state

log = get_logger("scheduler", service_name="Scheduler")

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _tick() -> None:
    entries = watch_state.all_watches()
    if not entries:
        log.debug("fallback tick: no watches registered")
        return

    log.info(
        "fallback tick | %d persisted mailbox(es) on this server (not necessarily the browser user)",
        len(entries),
    )
    for entry in entries:
        email_address = entry.get("email_address")
        if not email_address:
            continue
        owner = entry.get("owner_user_id")
        owner_hint = f"{str(owner)[:12]}…" if owner and len(str(owner)) > 12 else (owner or "legacy/no-owner")
        try:
            renewed = gmail_sync.renew_watch_if_needed(email_address)
            if not renewed:
                log.info(
                    "fallback scheduler incremental | mailbox=%s | linked_supabase_sub=%s",
                    email_address,
                    owner_hint,
                )
                gmail_sync.incremental_sync(email_address)
        except Exception as e:
            log.error("fallback sync failed for %s: %s", email_address, e)


def _run() -> None:
    from app.container import get_container

    interval = get_container().settings.mailpulse_fallback_sync_seconds
    log.info(
        "fallback scheduler started | interval=%ds (NOT 1-minute polling)",
        interval,
    )
    # Stagger the first run so we don't double-fire with a request right at boot.
    if _stop_event.wait(60):
        return
    while not _stop_event.is_set():
        try:
            _tick()
        except Exception as e:
            log.exception("scheduler tick crashed: %s", e)
        if _stop_event.wait(interval):
            return
    log.info("fallback scheduler stopped")


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    from app.container import get_container

    if not get_container().settings.mailpulse_fallback_scheduler_enabled:
        log.info("fallback scheduler skipped (MAILPULSE_FALLBACK_SCHEDULER_ENABLED=false)")
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_run, name="mailpulse-fallback-sync", daemon=True)
    _thread.start()


def stop() -> None:
    _stop_event.set()
