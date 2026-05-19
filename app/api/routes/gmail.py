"""HTTP endpoints for Gmail push notifications + incremental sync."""

from __future__ import annotations

import asyncio
import json
import queue as queue_mod

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from app.container import MailPulseContainer
from app.core import event_bus
from app.core.exceptions import ConflictException, EmailSyncException
from app.core.logging import get_logger
from app.core.middleware.auth import get_supabase_user_id, require_supabase_user
from app.core.rate_limit import limiter
from app.deps import get_mailpulse_container
from app.services import session_credentials, watch_state
from app.services.gmail_service import get_user_email

log = get_logger("gmail_routes", service_name="GmailRoutes")

public_gmail_router = APIRouter(prefix="/gmail", tags=["gmail"])


@public_gmail_router.post("/webhook")
async def gmail_webhook(
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    try:
        envelope = await request.json()
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("webhook: could not parse JSON body: %s", e)
        return {"acknowledged": True, "ignored": True, "reason": "bad-json"}

    log.info("webhook received | subscription=%s", envelope.get("subscription"))

    try:
        payload = container.gmail_sync.decode_pubsub_envelope(envelope)
    except ValueError as e:
        log.warning("webhook: invalid envelope (%s)", e)
        return {"acknowledged": True, "ignored": True, "reason": str(e)}

    email_address = payload["emailAddress"]
    push_history_id = str(payload.get("historyId"))

    if not watch_state.get(email_address):
        log.warning(
            "webhook: ignoring notification for unknown user %s (no watch state)",
            email_address,
        )
        return {"acknowledged": True, "ignored": True, "reason": "no-watch-state"}

    queued = container.gmail_sync.enqueue_incremental_sync(email_address)

    return {
        "acknowledged": True,
        "email_address": email_address,
        "history_id": push_history_id,
        "queued": queued,
    }

protected_gmail_router = APIRouter(
    prefix="/gmail",
    tags=["gmail"],
    dependencies=[Depends(require_supabase_user)],
)


def _require_google(request: Request) -> dict:
    return session_credentials.load_and_persist_fresh_for_request(request)


@protected_gmail_router.post("/watch")
@limiter.limit("10/minute")
def init_watch(
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    creds = _require_google(request)
    descriptor = container.gmail_sync.register_watch_for_user(
        creds,
        owner_user_id=get_supabase_user_id(request),
    )
    return {"success": True, **descriptor}


@protected_gmail_router.post("/sync")
@limiter.limit("10/minute")
def manual_sync(
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    creds = _require_google(request)
    try:
        email_address = get_user_email(creds)
    except EmailSyncException as e:
        raise EmailSyncException(str(e), code="EMAIL_RESOLVE_FAILED") from e

    if not watch_state.get(email_address):
        raise ConflictException(
            f"No active watch for {email_address}. Call POST /gmail/watch first.",
            code="NO_ACTIVE_WATCH",
        )

    queued = container.gmail_sync.enqueue_incremental_sync(email_address)
    return {"success": True, "email_address": email_address, "queued": queued}


@protected_gmail_router.post("/stop")
@limiter.limit("10/minute")
def stop(
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    creds = _require_google(request)
    try:
        email_address = get_user_email(creds)
    except EmailSyncException as e:
        raise EmailSyncException(str(e), code="EMAIL_RESOLVE_FAILED") from e
    try:
        container.gmail_sync.unregister_watch_for_user(email_address)
        return {"success": True, "email_address": email_address}
    except EmailSyncException:
        log.exception("stop watch failed")
        raise


@protected_gmail_router.get("/events")
async def gmail_events(
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    creds = _require_google(request)
    try:
        loop = asyncio.get_event_loop()
        email_address = await loop.run_in_executor(None, get_user_email, creds)
    except EmailSyncException as e:
        raise EmailSyncException(str(e), code="EMAIL_RESOLVE_FAILED") from e

    q = event_bus.subscribe(email_address)
    log.info(
        "SSE connected | %s | open_connections=%d",
        email_address,
        event_bus.active_connections(email_address),
    )

    async def stream():
        try:
            loops = 0
            while True:
                try:
                    data = q.get_nowait()
                    yield f"data: {json.dumps(data)}\n\n"
                    loops = 0
                except queue_mod.Empty:
                    await asyncio.sleep(1)
                    loops += 1
                    if loops >= 25:
                        yield ": keepalive\n\n"
                        loops = 0
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            event_bus.unsubscribe(email_address, q)
            log.info(
                "SSE disconnected | %s | open_connections=%d",
                email_address,
                event_bus.active_connections(email_address),
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@protected_gmail_router.get("/status")
def status(
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    creds = _require_google(request)
    try:
        email_address = get_user_email(creds)
    except EmailSyncException as e:
        raise EmailSyncException(str(e), code="EMAIL_RESOLVE_FAILED") from e

    entry = watch_state.get(email_address)
    return {
        "email_address": email_address,
        "configured_topic": container.settings.gmail_pubsub_topic,
        "watch_active": bool(entry),
        "history_id": entry.get("history_id") if entry else None,
        "watch_expiration": entry.get("watch_expiration") if entry else None,
        "last_synced_at": entry.get("last_synced_at") if entry else None,
        "topic_name": entry.get("topic_name") if entry else None,
    }
