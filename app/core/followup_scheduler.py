"""Periodically rebuilds the follow-up queue for each registered Gmail watch."""

from __future__ import annotations

import threading
import time
from typing import Optional

from app.core.logging import get_logger
from app.services import followup_service, watch_state

log = get_logger("followup_scheduler", service_name="FollowupScheduler")

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _tick() -> None:
    entries = list(watch_state.all_watches())
    if not entries:
        log.debug("follow-up tick: no watch entries")
        return
    for entry in entries:
        email_address = entry.get("email_address")
        creds = entry.get("credentials")
        if not email_address or not creds:
            continue
        try:
            followup_service.rebuild_for_account(email_address, creds)
        except Exception as e:
            log.error("follow-up rebuild failed for %s: %s", email_address, e)


def _run() -> None:
    from app.container import get_container

    interval = get_container().settings.mailpulse_followup_seconds
    log.info("follow-up scheduler started | interval=%ds", interval)
    if _stop_event.wait(45):
        return
    while not _stop_event.is_set():
        try:
            _tick()
        except Exception as e:
            log.exception("follow-up scheduler tick crashed: %s", e)
        if _stop_event.wait(interval):
            return
    log.info("follow-up scheduler stopped")


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_run, name="mailpulse-followups", daemon=True)
    _thread.start()


def stop() -> None:
    _stop_event.set()
