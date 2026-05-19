from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from app.container import MailPulseContainer
from app.core.exceptions import AuthException, EmailSyncException
from app.core.middleware.auth import require_supabase_user
from app.deps import get_mailpulse_container
from app.services import session_credentials
from app.services.gmail_service import get_user_email

router = APIRouter(prefix="/followups", dependencies=[Depends(require_supabase_user)])


def _session_account(request: Request, container: MailPulseContainer) -> str:
    creds = session_credentials.load_and_persist_fresh_for_request(request)
    try:
        return get_user_email(creds)
    except EmailSyncException as e:
        raise AuthException("Not authenticated", code="NOT_AUTHENTICATED", status_code=401) from e


@router.get("")
def list_followups(
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    email = _session_account(request, container)
    items = container.followup_service.list_followups(email)
    return {"followups": items}


@router.post("/{thread_id}/done")
def mark_followup_done(
    request: Request,
        thread_id: Annotated[str, Path(max_length=128, min_length=1, pattern=r"^[A-Za-z0-9_.\-]+$")],
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    email = _session_account(request, container)
    container.followup_service.dismiss_thread(email, thread_id)
    return {"ok": True, "thread_id": thread_id}
