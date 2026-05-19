"""Gmail push registration, incremental sync, and cache hydration."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from app.core import event_bus
from app.core.config import get_settings
from app.core.exceptions import ConflictException, EmailSyncException
from app.core.logging import get_logger
from app.services import email_cache, gmail_service, watch_state

log = get_logger("gmail_sync_service", service_name="GmailSyncService")

BOOTSTRAP_LIMIT = 5000
_BOOTSTRAP_PAGE_SIZE = 500
_MESSAGE_FETCH_RETRIES = 3
_MESSAGE_FETCH_BACKOFF = 0.7

_watch_init_lock = threading.Lock()
_watch_init_in_progress: set[str] = set()


class GmailSyncService:
    """Orchestrates watch lifecycle, history sync, and cache updates."""

    def __init__(self) -> None:
        settings = get_settings()
        workers = max(1, min(16, int(settings.gmail_sync_workers)))
        self._fetch_workers = max(1, min(32, int(settings.gmail_message_fetch_workers)))
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gmail-sync")
        self._job_lock = threading.Lock()
        self._jobs_in_progress: set[str] = set()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _submit_once(self, job_key: str, fn, *args, **kwargs) -> bool:
        with self._job_lock:
            if job_key in self._jobs_in_progress:
                log.info("sync job already queued/running | %s", job_key)
                return False
            self._jobs_in_progress.add(job_key)

        def _run_job() -> None:
            try:
                fn(*args, **kwargs)
            except Exception as e:
                log.exception("sync job failed | %s | %s", job_key, e)
            finally:
                with self._job_lock:
                    self._jobs_in_progress.discard(job_key)

        self._executor.submit(_run_job)
        return True

    def enqueue_bootstrap(
        self,
        creds: Dict[str, Any],
        email_address: str,
        *,
        owner_user_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> bool:
        max_messages = limit or get_settings().gmail_bootstrap_max_messages
        return self._submit_once(
            f"bootstrap:{email_address}",
            self.bootstrap_inbox,
            creds,
            email_address,
            max_messages,
            owner_user_id,
        )

    def enqueue_incremental_sync(self, email_address: str) -> bool:
        return self._submit_once(f"incremental:{email_address}", self.incremental_sync, email_address)

    def _persist_messages(
        self,
        owner_user_id: Optional[str],
        email_address: str,
        messages: List[Dict[str, Any]],
    ) -> None:
        if not owner_user_id or not messages:
            return
        try:
            from app.container import get_container

            get_container().gmail_message_index_repo.bulk_upsert_envelopes(
                str(owner_user_id), email_address, messages
            )
        except Exception as e:
            log.warning("Supabase message index persist failed: %s", e)

    def _delete_persisted_messages(
        self,
        owner_user_id: Optional[str],
        email_address: str,
        message_ids: List[str],
    ) -> None:
        if not owner_user_id or not message_ids:
            return
        try:
            from app.container import get_container

            get_container().gmail_message_index_repo.delete_messages(
                str(owner_user_id), email_address, message_ids
            )
        except Exception as e:
            log.warning("Supabase message index delete failed: %s", e)

    def _fetch_with_retry(self, creds: Dict[str, Any], message_id: str) -> Optional[Dict[str, Any]]:
        last_err: Optional[Exception] = None
        for attempt in range(1, _MESSAGE_FETCH_RETRIES + 1):
            try:
                return gmail_service.get_message_full(creds, message_id)
            except EmailSyncException:
                raise
            except Exception as e:
                last_err = e
                sleep_for = _MESSAGE_FETCH_BACKOFF * (2 ** (attempt - 1))
                log.warning(
                    "messages.get failed for %s (attempt %s/%s): %s — backing off %.1fs",
                    message_id, attempt, _MESSAGE_FETCH_RETRIES, e, sleep_for,
                )
                time.sleep(sleep_for)
        log.error("Giving up on message %s — last error: %s", message_id, last_err)
        return None

    def _hydrate_messages(
        self, creds: Dict[str, Any], email_address: str, message_ids: List[str]
    ) -> List[Dict[str, Any]]:
        if not message_ids:
            return []

        known = email_cache.known_message_ids(email_address)
        fresh_ids = [mid for mid in message_ids if mid not in known]
        skipped = len(message_ids) - len(fresh_ids)
        if skipped:
            log.info("dedup skipped %d already-cached messages", skipped)

        fetched: List[Dict[str, Any]] = []
        workers = min(self._fetch_workers, max(1, len(fresh_ids)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gmail-message") as pool:
            futures = {pool.submit(self._fetch_with_retry, creds, mid): mid for mid in fresh_ids}
            for future in as_completed(futures):
                try:
                    msg = future.result()
                except Exception as e:
                    log.warning("message hydration failed for %s: %s", futures[future], e)
                    continue
                if msg:
                    fetched.append(msg)
        if fetched:
            email_cache.upsert_messages(email_address, fetched)
        return fetched

    def register_watch_for_user(
        self,
        creds: Dict[str, Any],
        topic: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            email_address = gmail_service.get_user_email(creds)
        except Exception as e:
            raise EmailSyncException("Could not determine user email address from Gmail profile") from e
        if not email_address:
            raise EmailSyncException("Could not determine user email address from Gmail profile")

        try:
            watch_resp = gmail_service.start_watch(creds, topic_name=topic)
        except Exception as e:
            raise EmailSyncException("Failed to start Gmail watch", details={"stage": "start_watch"}) from e

        history_id = str(watch_resp.get("historyId", ""))
        expiration = watch_resp.get("expiration")
        topic_name = topic or gmail_service.get_pubsub_topic()

        watch_state.upsert_watch(
            email_address=email_address,
            history_id=history_id,
            watch_expiration=int(expiration) if expiration else None,
            topic_name=topic_name,
            credentials=creds,
            owner_user_id=owner_user_id,
        )

        queued = self.enqueue_bootstrap(
            creds,
            email_address,
            owner_user_id=owner_user_id,
            limit=get_settings().gmail_bootstrap_max_messages,
        )

        log.info(
            "watch ready | %s | historyId=%s | bootstrap_queued=%s", email_address, history_id, queued
        )
        return {
            "email_address": email_address,
            "history_id": history_id,
            "watch_expiration": expiration,
            "topic_name": topic_name,
            "bootstrap_queued": queued,
        }

    def ensure_watch_for_user(
        self,
        creds: Dict[str, Any],
        topic: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            email_address = gmail_service.get_user_email(creds)
        except Exception as e:
            raise EmailSyncException("Could not determine user email address from Gmail profile") from e
        if not email_address:
            raise EmailSyncException("Could not determine user email address from Gmail profile")

        existing = watch_state.get(email_address)
        owner = owner_user_id or (existing or {}).get("owner_user_id")
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
            return self.register_watch_for_user(creds, topic=topic, owner_user_id=str(owner) if owner else None)
        finally:
            with _watch_init_lock:
                _watch_init_in_progress.discard(email_address)

    def unregister_watch_for_user(self, email_address: str) -> None:
        entry = watch_state.get(email_address)
        if not entry:
            log.info("unregister_watch: no entry for %s", email_address)
            return
        try:
            gmail_service.stop_watch(entry["credentials"])
        finally:
            watch_state.remove(email_address)

    def renew_watch_if_needed(self, email_address: str) -> bool:
        entry = watch_state.get(email_address)
        if not entry:
            return False
        if not watch_state.is_expired(entry):
            return False
        log.info("watch for %s is near expiration — renewing", email_address)
        try:
            owner = entry.get("owner_user_id")
            self.register_watch_for_user(
                entry["credentials"],
                topic=entry.get("topic_name"),
                owner_user_id=str(owner) if owner else None,
            )
            return True
        except Exception as e:
            log.error("watch renewal failed for %s: %s", email_address, e)
            return False

    def bootstrap_inbox(
        self,
        creds: Dict[str, Any],
        email_address: str,
        limit: int = BOOTSTRAP_LIMIT,
        owner_user_id: Optional[str] = None,
    ) -> int:
        max_messages = max(1, min(50000, int(limit or BOOTSTRAP_LIMIT)))
        log.info("bootstrap inbox | %s | max_messages=%d", email_address, max_messages)
        total = 0
        page_token: Optional[str] = None
        while total < max_messages:
            batch_size = min(_BOOTSTRAP_PAGE_SIZE, max_messages - total)
            try:
                page = gmail_service.list_message_ids_page(
                    creds, limit=batch_size, page_token=page_token
                )
            except Exception as e:
                raise EmailSyncException("Bootstrap inbox list failed") from e
            ids = page.get("ids", []) or []
            if not ids:
                break
            msgs = self._hydrate_messages(creds, email_address, ids)
            for m in msgs:
                m.setdefault("internal_date", 0)
            self._persist_messages(owner_user_id, email_address, msgs)
            total += len(msgs)
            event_bus.notify(
                email_address,
                {
                    "type": "inbox_updated",
                    "added": len(msgs),
                    "updated": 0,
                    "removed": 0,
                    "syncing": bool(page.get("next_page_token") and total < max_messages),
                },
            )
            page_token = page.get("next_page_token")
            if not page_token:
                break
        if total:
            event_bus.notify(
                email_address,
                {"type": "inbox_sync_complete", "added": total, "updated": 0, "removed": 0},
            )
        watch_state.update_runtime_credentials(email_address, creds)
        log.info("bootstrap inbox complete | %s | imported=%d", email_address, total)
        return total

    def incremental_sync(self, email_address: str) -> Dict[str, Any]:
        entry = watch_state.get(email_address)
        if not entry:
            raise ConflictException(
                f"No watch state for {email_address}; call /gmail/watch first.",
                code="NO_WATCH_STATE",
            )

        creds = entry["credentials"]
        last_history_id = entry.get("history_id")

        if not last_history_id:
            log.info("no historyId on file — performing full bootstrap")
            owner_uid = entry.get("owner_user_id")
            seeded = self.bootstrap_inbox(
                creds,
                email_address,
                owner_user_id=str(owner_uid) if owner_uid else None,
                limit=get_settings().gmail_bootstrap_max_messages,
            )
            self.renew_watch_if_needed(email_address)
            watch_state.update_runtime_credentials(email_address, creds)
            return {"bootstrapped": seeded, "added": 0, "removed": 0, "updated": 0}

        owner_uid = entry.get("owner_user_id")
        log.info(
            "incremental sync | %s | owner_sub=%s | from historyId=%s",
            email_address,
            owner_uid or "legacy/no-owner",
            last_history_id,
        )
        try:
            history = gmail_service.list_history(creds, last_history_id)
        except Exception as e:
            raise EmailSyncException("history.list failed") from e

        if history.get("expired"):
            log.warning("historyId expired for %s — full resync", email_address)
            owner_uid = entry.get("owner_user_id")
            seeded = self.bootstrap_inbox(
                creds,
                email_address,
                owner_user_id=str(owner_uid) if owner_uid else None,
                limit=get_settings().gmail_bootstrap_max_messages,
            )
            self.renew_watch_if_needed(email_address)
            watch_state.update_runtime_credentials(email_address, creds)
            return {"bootstrapped": seeded, "added": 0, "removed": 0, "updated": 0}

        added = self._hydrate_messages(creds, email_address, history.get("added_ids", []))
        updated = self._hydrate_messages(creds, email_address, history.get("updated_ids", []))
        self._persist_messages(str(owner_uid) if owner_uid else None, email_address, added + updated)

        removed_ids = history.get("removed_ids", [])
        if removed_ids:
            email_cache.remove_messages(email_address, removed_ids)
            self._delete_persisted_messages(
                str(owner_uid) if owner_uid else None,
                email_address,
                removed_ids,
            )

        new_history_id = history.get("latest_history_id")
        if new_history_id and new_history_id != last_history_id:
            watch_state.update_history_id(email_address, new_history_id)

        log.info(
            "incremental sync done | %s | +%d -%d ~%d | newHistoryId=%s",
            email_address, len(added), len(removed_ids), len(updated), new_history_id,
        )

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

        watch_state.update_runtime_credentials(email_address, creds)

        return {
            "added": len(added),
            "removed": len(removed_ids),
            "updated": len(updated),
            "history_id": new_history_id,
        }

    def process_history_event(
        self, email_address: str, push_history_id: Optional[str]
    ) -> Dict[str, Any]:
        log.info("webhook trigger | %s | push_history_id=%s", email_address, push_history_id)
        return self.incremental_sync(email_address)

    @staticmethod
    def decode_pubsub_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
        import base64
        import binascii
        import json

        if not isinstance(envelope, dict) or "message" not in envelope:
            raise ValueError("Missing 'message' in Pub/Sub envelope")

        message = envelope["message"]
        raw_data: Optional[str] = message.get("data")
        if not raw_data:
            raise ValueError("Missing 'message.data'")

        try:
            decoded = base64.urlsafe_b64decode(raw_data + "==")
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"Could not base64-decode 'message.data': {e}") from e

        try:
            payload = json.loads(decoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"'message.data' was not valid JSON: {e}") from e

        if "emailAddress" not in payload or "historyId" not in payload:
            raise ValueError("Decoded payload missing 'emailAddress' or 'historyId'")

        return payload
