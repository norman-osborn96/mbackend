from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from app.core.logger import get_logger
from app.services import email_cache, gmail_sync, watch_state
from app.services.ai_classifier import generate_reply_suggestion
from app.services.gmail_service import fetch_emails, get_user_email, send_reply
from app.services.priority_engine import calculate_priority

router = APIRouter(prefix="/emails")
log = get_logger("emails_route")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def extract_email(sender: str) -> str:
    if not sender:
        return ""
    sender = sender.strip()
    if "<" in sender and ">" in sender:
        start = sender.find("<") + 1
        end = sender.find(">")
        return sender[start:end].strip().lower()
    return sender.lower()


def _enrich_with_priority(emails):
    """Run priority + AI-summary pipeline on a list of email envelopes."""
    enriched = []
    for email_obj in emails:
        clean_sender = extract_email(email_obj.get("sender", ""))
        email_obj["sender_email"] = clean_sender
        try:
            priority = calculate_priority({**email_obj, "sender": clean_sender})
        except Exception as e:
            log.error("priority calc failed: %s", e)
            priority = {"level": "LOW", "score": 0, "reasons": ["Fallback due to error"]}

        enriched.append(
            {
                **email_obj,
                "level": priority.get("level"),
                "score": priority.get("score"),
                "reasons": priority.get("reasons"),
                "confidence": priority.get("confidence"),
                "summary": priority.get("summary"),
                "ai_action": priority.get("ai_action"),
            }
        )
    return enriched


def _resolve_email_address(creds_dict) -> Optional[str]:
    """Resolve the user's Gmail address, returning None on any failure
    (we then transparently fall back to the legacy fetch path)."""
    try:
        return get_user_email(creds_dict)
    except Exception as e:
        log.warning("could not resolve user email: %s", e)
        return None


def _auto_start_gmail_watch(creds_dict: dict) -> None:
    """Lazy watch initialization for already-logged-in sessions."""
    try:
        gmail_sync.ensure_watch_for_user(creds_dict)
    except Exception as e:
        log.error("lazy Gmail watch setup failed: %s", e)


# ─── GET /emails ─────────────────────────────────────────────────────────────

@router.get("")
def get_emails(
    request: Request,
    background_tasks: BackgroundTasks,
    refresh: bool = False,
    since: Optional[int] = None,
    page: int = 1,
    limit: int = 50,
    pageToken: Optional[str] = None,
):
    """List emails with priority annotations.

    Strategy
    --------
    * If the user has a registered watch (push notifications on), we serve
      from the local cache and use `page` / `limit` for pagination.
        - On `refresh=true` we trigger an incremental sync first.
        - On `since=<epoch_ms>` we sync then return only emails newer than
          that timestamp (incremental refresh for the frontend button).
    * Otherwise we fall back to the legacy `fetch_emails` path with Gmail's
      native `pageToken` so existing clients keep working.
    """
    creds_dict = request.session.get("credentials")
    if not creds_dict:
        raise HTTPException(status_code=401, detail="Not authenticated")

    page = max(1, int(page or 1))
    limit = max(1, min(500, int(limit or 50)))

    email_address = _resolve_email_address(creds_dict)
    has_watch = bool(email_address and watch_state.get(email_address))

    # ── Push-based path: serve from local cache, sync incrementally on refresh
    if has_watch:
        if refresh or since is not None:
            log.info("refresh/since | running incremental sync for %s", email_address)
            try:
                gmail_sync.incremental_sync(email_address)
            except Exception as e:
                log.error("incremental sync failed (continuing from cache): %s", e)

        # If the cache is stale (last write > TTL ago) and we have no recent
        # webhook activity, do a quick incremental sync. This is cheap because
        # history.list returns nothing when there are no changes.
        elif not email_cache.is_first_page_fresh(email_address):
            try:
                gmail_sync.incremental_sync(email_address)
            except Exception as e:
                log.warning("opportunistic sync failed (serving cache): %s", e)

        # ── Incremental mode: return only emails newer than `since` ──
        if since is not None and since > 0:
            all_msgs = email_cache.list_all_messages(email_address)
            new_msgs = [m for m in all_msgs if int(m.get("internal_date") or 0) > since]
            enriched = _enrich_with_priority(new_msgs)
            log.info(
                "/emails (incremental) | %s | since=%d | new=%d",
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
            }

        page_data = email_cache.list_paginated(email_address, page=page, limit=limit)
        enriched = _enrich_with_priority(page_data["emails"])

        log.info(
            "/emails (push) | %s | page=%d limit=%d returned=%d total=%d",
            email_address, page, limit, len(enriched), page_data["total"],
        )
        return {
            "emails": enriched,
            "page": page,
            "limit": limit,
            "total": page_data["total"],
            "has_more": page_data["has_more"],
            "nextPageToken": None,  # legacy field, kept for backward compat
            "source": "push-cache",
            "mode": "full",
        }

    # ── Legacy path: original behaviour (Gmail-native pagination via pageToken)
    # If this is an older session that logged in before auto-watch existed,
    # initialize Gmail watch in the background. This removes the need to run
    # POST /gmail/watch manually after OAuth login.
    if email_address:
        log.info("no active Gmail watch for %s; scheduling lazy setup", email_address)
        background_tasks.add_task(_auto_start_gmail_watch, creds_dict)

    log.info(
        "/emails (legacy) | refresh=%s page=%d limit=%d pageToken=%s",
        refresh, page, limit, pageToken,
    )
    fetch_result = fetch_emails(
        creds_dict, force_refresh=refresh, limit=limit, page_token=pageToken
    )
    emails = fetch_result.get("emails", [])
    next_page_token = fetch_result.get("next_page_token")

    enriched = _enrich_with_priority(emails)
    return {
        "emails": enriched,
        "page": page,
        "limit": limit,
        "total": len(enriched),
        "has_more": bool(next_page_token),
        "nextPageToken": next_page_token,
        "source": "legacy-fetch",
    }


# ─── AI reply suggestion ─────────────────────────────────────────────────────

class SuggestReplyRequest(BaseModel):
    subject: str = ""
    snippet: str = ""
    sender: str = ""


@router.post("/suggest-reply")
def suggest_reply(body: SuggestReplyRequest, request: Request):
    creds_dict = request.session.get("credentials")
    if not creds_dict:
        raise HTTPException(status_code=401, detail="Not authenticated")

    suggestion = generate_reply_suggestion(
        subject=body.subject,
        snippet=body.snippet,
        sender=body.sender,
    )
    if not suggestion:
        raise HTTPException(status_code=500, detail="Could not generate reply suggestion")
    return {"suggestion": suggestion}


# ─── Send reply ──────────────────────────────────────────────────────────────

class SendReplyRequest(BaseModel):
    to: str
    subject: str
    body: str
    thread_id: str = ""
    message_id_header: str = ""


@router.post("/send-reply")
def send_email_reply(body: SendReplyRequest, request: Request):
    creds_dict = request.session.get("credentials")
    if not creds_dict:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        result = send_reply(
            creds_dict=creds_dict,
            to=body.to,
            subject=body.subject,
            body=body.body,
            thread_id=body.thread_id,
            reply_to_message_id=body.message_id_header,
        )
        return {"success": True, "message_id": result.get("id", "")}
    except Exception as e:
        log.exception("send reply error")
        raise HTTPException(status_code=500, detail=str(e))
