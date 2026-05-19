from fastapi import APIRouter, Depends, Request

from app.container import MailPulseContainer
from app.core.middleware.auth import require_supabase_user
from app.deps import get_mailpulse_container
from app.services import session_credentials

router = APIRouter(prefix="/dashboard", dependencies=[Depends(require_supabase_user)])


@router.get("")
def get_dashboard(
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
    refresh: bool = False,
):
    creds_dict = session_credentials.load_and_persist_fresh_for_request(request)
    return container.insight_service.dashboard_counts(creds_dict)


@router.get("/stats")
def get_stats(
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
    refresh: bool = False,
):
    creds_dict = session_credentials.load_and_persist_fresh_for_request(request)
    return container.insight_service.dashboard_stats(creds_dict)
