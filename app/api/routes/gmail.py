"""HTTP endpoints for Gmail push notifications + incremental sync.

Endpoints
---------
POST /gmail/watch     - Initialize a Gmail watch for the logged-in user.
POST /gmail/webhook   - Public endpoint Pub/Sub posts notifications to.
POST /gmail/sync      - Manually trigger incremental sync (debug / fallback).
POST /gmail/stop      - Cancel the watch.
GET  /gmail/status    - Inspect the current watch + history state.

Example Pub/Sub payload `POST /gmail/webhook` receives:

    {
      "message": {
        "data": "eyJlbWFpbEFkZHJlc3MiOiAidXNlckBnbWFpbC5jb20iLCAiaGlzdG9yeUlkIjogIjEyMzQ1NjcifQ==",
        "messageId": "1111",
        "publishTime": "2026-04-29T11:00:00Z"
      },
      "subscription": "projects/<id>/subscriptions/<name>"
    }

`message.data` is the base64url-encoded JSON
`{"emailAddress": "user@gmail.com", "historyId": "1234567"}`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import json
import queue as queue_mod
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core import event_bus
from app.core.logger import get_logger
from app.services import gmail_sync, watch_state
from app.services.gmail_service import GMAIL_PUBSUB_TOPIC

log = get_logger("gmail_routes")
router = APIRouter(prefix="/gmail", tags=["gmail"])


# ─── 1. Initialize watch ─────────────────────────────────────────────────────

@router.post("/watch")
def init_watch(request: Request):
    """Register a Gmail push-notification watch for the logged-in user."""
    creds = request.session.get("credentials")
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        descriptor = gmail_sync.register_watch_for_user(creds)
        return {"success": True, **descriptor}
    except Exception as e:
        log.exception("watch registration failed")
        raise HTTPException(status_code=500, detail=f"Failed to start watch: {e}")


# ─── 2. Pub/Sub webhook ──────────────────────────────────────────────────────

def _decode_pubsub_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and decode the inner Gmail notification from a Pub/Sub push."""
    if not isinstance(envelope, dict) or "message" not in envelope:
        raise ValueError("Missing 'message' in Pub/Sub envelope")

    message = envelope["message"]
    raw_data: Optional[str] = message.get("data")
    if not raw_data:
        raise ValueError("Missing 'message.data'")

    try:
        decoded = base64.urlsafe_b64decode(raw_data + "==")  # tolerate missing padding
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"Could not base64-decode 'message.data': {e}")

    try:
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"'message.data' was not valid JSON: {e}")

    if "emailAddress" not in payload or "historyId" not in payload:
        raise ValueError("Decoded payload missing 'emailAddress' or 'historyId'")

    return payload


@router.post("/webhook")
async def gmail_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive a Pub/Sub push notification from Gmail.

    We always return 200 quickly so Pub/Sub doesn't retry while a slow sync is
    still in-flight. The actual incremental fetch runs in a background task.
    """
    try:
        envelope = await request.json()
    except Exception as e:
        log.warning("webhook: could not parse JSON body: %s", e)
        # Acknowledge so Pub/Sub doesn't retry forever on malformed bodies.
        return {"acknowledged": True, "ignored": True, "reason": "bad-json"}

    log.info("webhook received | subscription=%s", envelope.get("subscription"))

    try:
        payload = _decode_pubsub_envelope(envelope)
    except ValueError as e:
        log.warning("webhook: invalid envelope (%s)", e)
        return {"acknowledged": True, "ignored": True, "reason": str(e)}

    email_address = payload["emailAddress"]
    push_history_id = str(payload.get("historyId"))

    # If we never set up a watch for this user, we can't sync. Just ack.
    if not watch_state.get(email_address):
        log.warning(
            "webhook: ignoring notification for unknown user %s (no watch state)",
            email_address,
        )
        return {"acknowledged": True, "ignored": True, "reason": "no-watch-state"}

    # Run the actual sync off the request thread so Pub/Sub gets a fast 200.
    background_tasks.add_task(
        _safe_process_history_event, email_address, push_history_id
    )

    return {"acknowledged": True, "email_address": email_address, "history_id": push_history_id}


def _safe_process_history_event(email_address: str, push_history_id: str) -> None:
    """Wrap process_history_event in a try/except so a webhook task failure
    never blows up the FastAPI background runner. Renew watch on auth errors."""
    try:
        gmail_sync.process_history_event(email_address, push_history_id)
    except Exception as e:
        log.exception("webhook background sync failed for %s: %s", email_address, e)
        # If the failure was because the watch expired, try to renew it so the
        # next push has a chance of working.
        try:
            gmail_sync.renew_watch_if_needed(email_address)
        except Exception as renew_err:
            log.error("renew attempt also failed for %s: %s", email_address, renew_err)


# ─── 3. Manual sync trigger (debug / fallback) ───────────────────────────────

@router.post("/sync")
def manual_sync(request: Request):
    """Manually trigger an incremental sync for the logged-in user."""
    creds = request.session.get("credentials")
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Determine the user's email so we know which watch entry to sync.
    try:
        from app.services.gmail_service import get_user_email

        email_address = get_user_email(creds)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not resolve user email: {e}")

    if not watch_state.get(email_address):
        raise HTTPException(
            status_code=409,
            detail=f"No active watch for {email_address}. Call POST /gmail/watch first.",
        )

    try:
        result = gmail_sync.incremental_sync(email_address)
        return {"success": True, "email_address": email_address, **result}
    except Exception as e:
        log.exception("manual sync failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─── 4. Stop watch ───────────────────────────────────────────────────────────

@router.post("/stop")
def stop(request: Request):
    creds = request.session.get("credentials")
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        from app.services.gmail_service import get_user_email

        email_address = get_user_email(creds)
        gmail_sync.unregister_watch_for_user(email_address)
        return {"success": True, "email_address": email_address}
    except Exception as e:
        log.exception("stop watch failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─── 5. Server-Sent Events (real-time inbox update signal) ──────────────────

@router.get("/events")
async def gmail_events(request: Request):
    """SSE stream that pushes a signal whenever this user's inbox changes.

    The browser opens one persistent EventSource connection. When
    incremental_sync finds new / removed emails it calls event_bus.notify(),
    which places a message on this user's queue. The stream sends it to the
    browser immediately — no polling needed.

    !! Do NOT call request.is_disconnected() inside the generator. !!
    In Starlette's ASGI implementation, calling is_disconnected() from inside
    a StreamingResponse generator reads from the receive channel, which may
    return True immediately and kill the connection before any events are sent.
    Instead we rely on asyncio.CancelledError: FastAPI cancels the generator
    coroutine automatically when the HTTP connection closes.
    """
    creds = request.session.get("credentials")
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        from app.services.gmail_service import get_user_email
        loop = asyncio.get_event_loop()
        email_address = await loop.run_in_executor(None, get_user_email, creds)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not resolve user email: {e}")

    q = event_bus.subscribe(email_address)
    log.info("SSE connected | %s | open_connections=%d",
             email_address, event_bus.active_connections(email_address))

    async def stream():
        try:
            loops = 0
            while True:
                try:
                    # Poll the queue non-blockingly so we don't exhaust the FastAPI
                    # or asyncio thread pools. Threads blocked in run_in_executor
                    # cannot be cancelled when the client disconnects!
                    data = q.get_nowait()
                    yield f"data: {json.dumps(data)}\n\n"
                    loops = 0
                except queue_mod.Empty:
                    await asyncio.sleep(1)
                    loops += 1
                    # Send a keepalive comment every 25 seconds
                    if loops >= 25:
                        yield ": keepalive\n\n"
                        loops = 0
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnected — FastAPI cancels the generator.
            pass
        finally:
            event_bus.unsubscribe(email_address, q)
            log.info("SSE disconnected | %s | open_connections=%d",
                     email_address, event_bus.active_connections(email_address))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─── 6. Status ───────────────────────────────────────────────────────────────

@router.get("/status")
def status(request: Request):
    creds = request.session.get("credentials")
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        from app.services.gmail_service import get_user_email

        email_address = get_user_email(creds)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not resolve user email: {e}")

    entry = watch_state.get(email_address)
    return {
        "email_address": email_address,
        "configured_topic": GMAIL_PUBSUB_TOPIC,
        "watch_active": bool(entry),
        "history_id": entry.get("history_id") if entry else None,
        "watch_expiration": entry.get("watch_expiration") if entry else None,
        "last_synced_at": entry.get("last_synced_at") if entry else None,
        "topic_name": entry.get("topic_name") if entry else None,
    }
