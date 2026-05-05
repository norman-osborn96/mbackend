"""Persistent follow-up queue keyed by Gmail account.

Items are rebuilt periodically by ``followup_scheduler`` from inbox messages
(high priority + AI ``REQUIRES_REPLY``). Threads marked ``done`` are suppressed
until that id ages out of the dismissed list."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.core.logger import get_logger
from app.services import gmail_service
from app.services.priority_engine import calculate_priority

log = get_logger("followup_service")

_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "follow_up_queue.json"))
_LOCK = threading.Lock()
_MAX_DISMISSED = 400


def _load() -> Dict[str, Any]:
    if not os.path.exists(_PATH):
        return {"accounts": {}}
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {"accounts": {}}
    except Exception as e:
        log.warning("follow-up queue read failed: %s", e)
        return {"accounts": {}}


def _save(data: Dict[str, Any]) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _PATH)


def _internal_date_iso(internal_ms: int) -> str:
    if not internal_ms:
        return ""
    try:
        return datetime.fromtimestamp(internal_ms / 1000.0, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def list_items(account_email: str) -> List[Dict[str, Any]]:
    with _LOCK:
        acc = (_load().get("accounts") or {}).get(account_email)
        if not acc:
            return []
        return list(acc.get("items") or [])


def dismiss_thread(account_email: str, thread_id: str) -> None:
    if not account_email or not thread_id:
        return
    with _LOCK:
        data = _load()
        accounts = data.setdefault("accounts", {})
        acc = accounts.setdefault(
            account_email,
            {"items": [], "dismissed_thread_ids": []},
        )
        dismissed: List[str] = list(acc.get("dismissed_thread_ids") or [])
        if thread_id not in dismissed:
            dismissed.append(thread_id)
        if len(dismissed) > _MAX_DISMISSED:
            dismissed = dismissed[-_MAX_DISMISSED:]
        acc["dismissed_thread_ids"] = dismissed
        acc["items"] = [i for i in (acc.get("items") or []) if i.get("thread_id") != thread_id]
        _save(data)
    log.info("follow-up dismissed | %s | thread=%s", account_email, thread_id[:16])


def rebuild_for_account(email_address: str, creds: Dict[str, Any]) -> int:
    """Scan recent inbox threads and merge follow-up candidates."""
    dismissed_set: set[str] = set()
    with _LOCK:
        data = _load()
        acc = (data.get("accounts") or {}).get(email_address) or {}
        dismissed_set = set(acc.get("dismissed_thread_ids") or [])

    try:
        result = gmail_service.fetch_emails(creds, force_refresh=False, limit=75)
    except Exception as e:
        log.error("follow-up rebuild fetch failed %s: %s", email_address, e)
        return 0
    emails = (result or {}).get("emails") if isinstance(result, dict) else None
    if not emails:
        return 0

    # Latest message per thread
    thread_latest: Dict[str, Dict[str, Any]] = {}
    for em in emails:
        tid = str(em.get("thread_id") or "")
        if not tid:
            continue
        internal = int(em.get("internal_date") or 0)
        prev = thread_latest.get(tid)
        if prev is None or internal >= int(prev.get("internal_date") or 0):
            thread_latest[tid] = em

    candidates: List[Dict[str, Any]] = []
    for tid, em in thread_latest.items():
        if tid in dismissed_set:
            continue
        try:
            pr = calculate_priority(em)
        except Exception as e:
            log.debug("priority failed for thread %s: %s", tid[:12], e)
            continue
        level = (pr.get("level") or "LOW").upper()
        ai_action = (pr.get("ai_action") or "FYI").upper()
        if level != "HIGH" and ai_action != "REQUIRES_REPLY":
            continue
        iso = _internal_date_iso(int(em.get("internal_date") or 0))
        item = {
            "id": tid,
            "thread_id": tid,
            "email_id": str(em.get("id") or ""),
            "subject": em.get("subject") or "",
            "sender": em.get("sender") or "",
            "last_message_at": iso,
            "requires_reply": ai_action == "REQUIRES_REPLY",
            "priority": level,
        }
        candidates.append(item)

    candidates.sort(
        key=lambda x: thread_latest.get(x["thread_id"], {}).get("internal_date") or 0,
        reverse=True,
    )

    with _LOCK:
        data = _load()
        accounts = data.setdefault("accounts", {})
        existing = accounts.get(email_address) or {}
        acc = {
            "items": candidates,
            "dismissed_thread_ids": list(existing.get("dismissed_thread_ids") or []),
            "updated_at": time.time(),
        }
        if len(acc["dismissed_thread_ids"]) > _MAX_DISMISSED:
            acc["dismissed_thread_ids"] = acc["dismissed_thread_ids"][-_MAX_DISMISSED:]
        accounts[email_address] = acc
        _save(data)

    log.info("follow-up queue rebuilt | %s | %d items", email_address, len(candidates))
    return len(candidates)
