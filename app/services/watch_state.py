"""Persistent watch-state store with optional Fernet-encrypted OAuth blobs on disk."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Iterable, Optional

from app.core.logging import get_logger
from app.services import oauth_token_crypto

log = get_logger("watch_state", service_name="WatchState")

_STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "gmail_watch_state.json")
_STATE_PATH = os.path.abspath(_STATE_PATH)
_lock = threading.Lock()


def _load_raw() -> Dict[str, Any]:
    if not os.path.exists(_STATE_PATH):
        return {}
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError, TypeError) as e:
        log.warning("Failed to read watch state file: %s — starting empty", e)
        return {}


def _write_raw(data: Dict[str, Any]) -> None:
    tmp_path = _STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, _STATE_PATH)


def _hydrate_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Build a runtime entry with plaintext ``credentials`` for callers."""
    out = dict(entry)
    if "credentials_enc" in out and oauth_token_crypto.is_configured():
        try:
            out["credentials"] = oauth_token_crypto.decrypt_credentials_blob(out["credentials_enc"])
        except Exception as e:
            log.error("watch_state: failed to decrypt credentials: %s", e)
            out.pop("credentials", None)
    elif "credentials" in out and isinstance(out["credentials"], dict):
        pass
    return out


def _serialize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Strip plaintext credentials for JSON persistence."""
    disk: Dict[str, Any] = {k: v for k, v in entry.items() if k not in ("credentials", "credentials_enc")}
    creds = entry.get("credentials")
    if isinstance(creds, dict):
        if oauth_token_crypto.is_configured():
            disk["credentials_enc"] = oauth_token_crypto.encrypt_credentials_blob(creds)
        else:
            disk["credentials"] = creds
    elif "credentials_enc" in entry:
        disk["credentials_enc"] = entry["credentials_enc"]
    if entry.get("owner_user_id"):
        disk["owner_user_id"] = entry["owner_user_id"]
    return disk


def _load() -> Dict[str, Any]:
    raw = _load_raw()
    return {email: _hydrate_entry(entry) for email, entry in raw.items()}


def _save(data_runtime: Dict[str, Any]) -> None:
    raw = {email: _serialize_entry(entry) for email, entry in data_runtime.items()}
    _write_raw(raw)


def upsert_watch(
    email_address: str,
    history_id: str,
    watch_expiration: Optional[int],
    topic_name: str,
    credentials: Dict[str, Any],
    owner_user_id: Optional[str] = None,
) -> None:
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
        if owner_user_id:
            existing["owner_user_id"] = str(owner_user_id)
        data[email_address] = existing
        _save(data)
        log.info(
            "watch upserted for %s | historyId=%s | expires=%s",
            email_address,
            history_id,
            existing.get("watch_expiration"),
        )


def update_runtime_credentials(email_address: str, credentials: Dict[str, Any]) -> None:
    """Persist refreshed Google OAuth tokens for a watch entry."""
    with _lock:
        data = _load()
        if email_address not in data:
            return
        data[email_address]["credentials"] = credentials
        _save(data)


def update_history_id(email_address: str, history_id: str) -> None:
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
    exp_ms = entry.get("watch_expiration")
    if not exp_ms:
        return True
    return (exp_ms / 1000.0) - skew_seconds <= time.time()
