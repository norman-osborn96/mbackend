"""Inbox listing, enrichment, reply draft, and send-reply orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from app.core.exceptions import AuthException, EmailSyncException
from app.core.logging import get_logger
from app.services import email_cache, watch_state
from app.services.ai_classifier_service import AIClassifierService
from app.services.classification_service import ClassificationService
from app.services.gmail_service import fetch_emails, get_user_email, send_reply
from app.services.gmail_sync_service import GmailSyncService
from app.services.priority_engine import extract_email

if TYPE_CHECKING:
    from app.repositories.gmail_message_index_repository import GmailMessageIndexRepository
    from app.services.bucket_engine import BucketEngine

log = get_logger("email_service", service_name="EmailService")


class EmailService:
    def __init__(
        self,
        classification_service: ClassificationService,
        gmail_sync_service: GmailSyncService,
        ai_classifier: AIClassifierService,
        gmail_message_index_repo: Optional["GmailMessageIndexRepository"] = None,
        bucket_engine: Optional["BucketEngine"] = None,
    ) -> None:
        self._classification = classification_service
        self._gmail_sync = gmail_sync_service
        self._ai = ai_classifier
        self._msg_index = gmail_message_index_repo
        self._bucket_engine = bucket_engine

    def _require_creds(self, creds_dict: Optional[dict]) -> dict:
        if not creds_dict:
            raise AuthException("Not authenticated", code="NOT_AUTHENTICATED", status_code=401)
        return creds_dict

    def _enrich_with_priority(
        self, emails: List[dict], account_email: Optional[str] = None
    ) -> tuple[List[dict], bool]:
        if account_email and emails:
            self._ai.maybe_warm_cxo_profile(account_email, emails[:20])
        prefetch = self._ai.build_ai_prefetch(account_email, emails) if account_email else {}
        needs_profile_setup = False

        vip_list: List[str] = []
        medium_list: List[str] = []
        try:
            vip_list = self._classification.load_vip_senders()
            medium_list = self._classification.load_medium_senders()
        except Exception:
            pass

        enriched = []
        for email_obj in emails:
            clean_sender = extract_email(email_obj.get("sender", ""))
            email_obj["sender_email"] = clean_sender
            pid = str(email_obj.get("id") or "")
            try:
                pe = {
                    **email_obj,
                    "sender": clean_sender,
                    "account_email": account_email,
                }
                if pid and pid in prefetch:
                    pe["_ai_prefetch"] = prefetch[pid]
                priority = self._classification.calculate_priority(
                    pe,
                    vip_senders=vip_list,
                    medium_senders=medium_list,
                )
            except Exception as e:
                log.error("priority calc failed: %s", e)
                priority = {"level": "LOW", "score": 0, "reasons": ["Fallback due to error"]}

            if priority.get("needs_profile_setup"):
                needs_profile_setup = True

            enriched.append(
                {
                    **email_obj,
                    "account_email": account_email,
                    "level": priority.get("level"),
                    "score": priority.get("score"),
                    "reasons": priority.get("reasons"),
                    "confidence": priority.get("confidence"),
                    "summary": priority.get("summary"),
                    "ai_action": priority.get("ai_action"),
                    "ai_category": priority.get("ai_category") or "",
                }
            )
        return enriched, needs_profile_setup

    def _assign_buckets_if_possible(self, emails: List[dict], supabase_user_id: str) -> None:
        if not emails or not supabase_user_id or not self._bucket_engine:
            return
        try:
            self._bucket_engine.assign_emails_batch(emails, supabase_user_id)
        except Exception as e:
            log.warning("bucket enrichment skipped: %s", e)

    def _resolve_email_address(self, creds_dict: dict) -> Optional[str]:
        try:
            return get_user_email(creds_dict)
        except Exception as e:
            log.warning("could not resolve user email: %s", e)
            return None

    def _hydrate_local_cache_from_supabase(self, supabase_user_id: str, mailbox: str) -> None:
        if not self._msg_index or not supabase_user_id or not mailbox:
            return
        if email_cache.list_all_messages(mailbox):
            return
        envs = self._msg_index.list_envelopes(supabase_user_id, mailbox, limit=800)
        if envs:
            email_cache.upsert_messages(mailbox, envs)
            log.info("hydrated email_cache from Supabase | %s | n=%d", mailbox, len(envs))

    def auto_start_gmail_watch(self, creds_dict: dict, owner_user_id: str = "") -> None:
        try:
            self._gmail_sync.ensure_watch_for_user(
                creds_dict,
                owner_user_id=owner_user_id or None,
            )
        except Exception as e:
            log.error("lazy Gmail watch setup failed: %s", e)

    def _page_from_token(self, page_token: Optional[str], limit: int) -> tuple[int, int]:
        if not page_token:
            return 1, 0
        token = str(page_token)
        if token.startswith("offset:"):
            try:
                offset = max(0, int(token.split(":", 1)[1]))
                return (offset // max(1, limit)) + 1, offset
            except ValueError:
                return 1, 0
        return 1, 0

    def list_emails(
        self,
        creds_dict: Optional[dict],
        *,
        refresh: bool,
        since: Optional[int],
        page: int,
        limit: int,
        page_token: Optional[str],
        schedule_lazy_watch: Optional[Callable[[dict], None]] = None,
        supabase_user_id: str = "",
    ) -> Dict[str, Any]:
        creds = self._require_creds(creds_dict)
        limit = max(1, min(500, int(limit or 50)))
        token_page, offset = self._page_from_token(page_token, limit)
        page = token_page if page_token else max(1, int(page or 1))

        email_address = self._resolve_email_address(creds)
        if email_address and supabase_user_id:
            self._hydrate_local_cache_from_supabase(supabase_user_id, email_address)

        has_watch = bool(email_address and watch_state.get(email_address))

        if has_watch:
            if refresh or since is not None:
                queued = self._gmail_sync.enqueue_incremental_sync(email_address)
                log.info("refresh/since | queued incremental sync for %s | queued=%s", email_address, queued)
            elif not email_cache.is_first_page_fresh(email_address):
                self._gmail_sync.enqueue_incremental_sync(email_address)

            if since is not None and since > 0:
                all_msgs = email_cache.list_all_messages(email_address)
                new_msgs = [m for m in all_msgs if int(m.get("internal_date") or 0) > since]
                enriched, need_pf = self._enrich_with_priority(new_msgs, email_address)
                self._assign_buckets_if_possible(enriched, supabase_user_id)
                log.info(
                    "/emails incremental | %s | since=%s | new=%s",
                    email_address, since, len(enriched),
                )
                return {
                    "emails": enriched,
                    "new_count": len(enriched),
                    "page": 1,
                    "limit": len(enriched),
                    "total": len(all_msgs),
                    "has_more": False,
                    "nextPageToken": None,
                    "source": "push-cache",
                    "mode": "incremental",
                    "needs_profile_setup": need_pf,
                    "syncing": True,
                }

            page_data: Dict[str, Any]
            if self._msg_index and supabase_user_id and email_address:
                page_data = self._msg_index.list_envelopes_page(
                    supabase_user_id,
                    email_address,
                    limit=limit,
                    offset=offset,
                )
                if page_data["emails"]:
                    email_cache.upsert_messages(email_address, page_data["emails"])
                    page_data["total"] = offset + len(page_data["emails"]) + (1 if page_data["has_more"] else 0)
                else:
                    page_data = email_cache.list_paginated(email_address, page=page, limit=limit)
                    page_data["next_offset"] = page * limit if page_data.get("has_more") else None
            else:
                page_data = email_cache.list_paginated(email_address, page=page, limit=limit)
                page_data["next_offset"] = page * limit if page_data.get("has_more") else None
            enriched, need_pf = self._enrich_with_priority(page_data["emails"], email_address)
            self._assign_buckets_if_possible(enriched, supabase_user_id)
            log.info(
                "/emails push | %s | page=%s limit=%s returned=%s total=%s",
                email_address, page, limit, len(enriched), page_data["total"],
            )
            return {
                "emails": enriched,
                "page": page,
                "limit": limit,
                "total": page_data["total"],
                "has_more": page_data["has_more"],
                "nextPageToken": (
                    f"offset:{page_data['next_offset']}" if page_data.get("next_offset") is not None else None
                ),
                "source": "push-cache",
                "mode": "full",
                "needs_profile_setup": need_pf,
                "syncing": True,
            }

        if email_address and schedule_lazy_watch:
            log.info("no active Gmail watch for %s; scheduling lazy setup", email_address)
            schedule_lazy_watch(creds)

        log.info(
            "/emails legacy | refresh=%s page=%s limit=%s pageToken=%s",
            refresh, page, limit, page_token,
        )
        fetch_result = fetch_emails(
            creds, force_refresh=refresh, limit=limit, page_token=page_token
        )
        emails = fetch_result.get("emails", [])
        next_page_token = fetch_result.get("next_page_token")
        enriched, need_pf = self._enrich_with_priority(emails, email_address)
        self._assign_buckets_if_possible(enriched, supabase_user_id)
        return {
            "emails": enriched,
            "page": page,
            "limit": limit,
            "total": len(enriched),
            "has_more": bool(next_page_token),
            "nextPageToken": next_page_token,
            "source": "legacy-fetch",
            "needs_profile_setup": need_pf,
        }

    def suggest_reply(
        self,
        creds_dict: Optional[dict],
        *,
        subject: str,
        snippet: str,
        sender: str,
        regenerate: bool = False,
    ) -> str:
        self._require_creds(creds_dict)
        return self._ai.generate_reply_suggestion(
            subject=subject,
            snippet=snippet,
            sender=sender,
            regenerate=regenerate,
        )

    def send_email_reply(
        self,
        creds_dict: Optional[dict],
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str,
        message_id_header: str,
    ) -> Dict[str, Any]:
        creds = self._require_creds(creds_dict)
        try:
            result = send_reply(
                creds_dict=creds,
                to=to,
                subject=subject,
                body=body,
                thread_id=thread_id,
                reply_to_message_id=message_id_header,
            )
            return {"success": True, "message_id": result.get("id", "")}
        except EmailSyncException:
            raise
        except Exception as e:
            log.exception("send reply error")
            raise EmailSyncException(str(e), details={"stage": "send_reply"}) from e
