from fastapi import APIRouter, Depends

from app.container import MailPulseContainer
from app.core.middleware.auth import require_supabase_user
from app.deps import get_mailpulse_container

from app.schemas.http_models import PrioritySenderBody

router = APIRouter(prefix="/priority", tags=["priority"], dependencies=[Depends(require_supabase_user)])


@router.get("/vip")
def get_vip_senders_api(container: MailPulseContainer = Depends(get_mailpulse_container)):
    data = container.classification.load_vip_senders()
    return {"success": True, "data": data}


@router.post("/vip")
def add_vip_sender_api(
    payload: PrioritySenderBody,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    container.classification.save_vip_sender(str(payload.email))
    return {"success": True}


@router.delete("/vip")
def delete_vip_sender_api(
    payload: PrioritySenderBody,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    container.classification.remove_vip_sender(str(payload.email))
    return {"success": True}


@router.get("/medium")
def get_medium_senders_api(container: MailPulseContainer = Depends(get_mailpulse_container)):
    data = container.classification.load_medium_senders()
    return {"success": True, "data": data}


@router.post("/medium")
def add_medium_sender_api(
    payload: PrioritySenderBody,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    container.classification.save_medium_sender(str(payload.email))
    return {"success": True}


@router.delete("/medium")
def delete_medium_sender_api(
    payload: PrioritySenderBody,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    container.classification.remove_medium_sender(str(payload.email))
    return {"success": True}
