"""Shim module so background jobs can call Gmail sync without holding a container reference.

Delegates to :class:`GmailSyncService` on the process-wide container (set at app startup).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.logging import get_logger

log = get_logger("gmail_sync", service_name="gmail_sync")


def register_watch_for_user(creds: Dict[str, Any], topic: Optional[str] = None) -> Dict[str, Any]:
    from app.container import get_container

    return get_container().gmail_sync.register_watch_for_user(creds, topic=topic)


def ensure_watch_for_user(creds: Dict[str, Any], topic: Optional[str] = None) -> Dict[str, Any]:
    from app.container import get_container

    return get_container().gmail_sync.ensure_watch_for_user(creds, topic=topic)


def unregister_watch_for_user(email_address: str) -> None:
    from app.container import get_container

    get_container().gmail_sync.unregister_watch_for_user(email_address)


def renew_watch_if_needed(email_address: str) -> bool:
    from app.container import get_container

    return get_container().gmail_sync.renew_watch_if_needed(email_address)


def bootstrap_inbox(creds: Dict[str, Any], email_address: str, limit: int = 100) -> int:
    from app.container import get_container

    return get_container().gmail_sync.bootstrap_inbox(creds, email_address, limit=limit)


def incremental_sync(email_address: str) -> Dict[str, Any]:
    from app.container import get_container

    return get_container().gmail_sync.incremental_sync(email_address)


def process_history_event(email_address: str, push_history_id: Optional[str]) -> Dict[str, Any]:
    from app.container import get_container

    return get_container().gmail_sync.process_history_event(email_address, push_history_id)
