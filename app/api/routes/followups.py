from fastapi import APIRouter, HTTPException, Request

from app.core.logger import get_logger
from app.services.gmail_service import get_user_email
from app.services import followup_service

router = APIRouter(prefix="/followups")
log = get_logger("followups_route")


def _session_account(request: Request) -> str:
    creds = request.session.get("credentials")
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return get_user_email(creds)
    except Exception as e:
        log.warning("could not resolve user email: %s", e)
        raise HTTPException(status_code=401, detail="Not authenticated")


@router.get("")
def list_followups(request: Request):
    """Return pending follow-ups for the signed-in Gmail user."""
    email = _session_account(request)
    items = followup_service.list_items(email)
    return {"followups": items}


@router.post("/{thread_id}/done")
def mark_followup_done(request: Request, thread_id: str):
    """Dismiss a thread from the follow-up list (persistent)."""
    email = _session_account(request)
    followup_service.dismiss_thread(email, thread_id)
    return {"ok": True, "thread_id": thread_id}
