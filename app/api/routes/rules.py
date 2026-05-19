from fastapi import APIRouter, Depends

from app.container import MailPulseContainer
from app.core.middleware.auth import require_supabase_user
from app.deps import get_mailpulse_container
from app.schemas.http_models import SenderEmailRuleBody

router = APIRouter(prefix="/rules", tags=["rules"], dependencies=[Depends(require_supabase_user)])


@router.get("/high-priority-senders")
def list_vip(container: MailPulseContainer = Depends(get_mailpulse_container)):
    return {"high_priority_senders": container.classification.load_vip_senders()}


@router.post("/high-priority-senders")
def add_vip(
    payload: SenderEmailRuleBody,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    container.classification.save_vip_sender(str(payload.sender_email))
    return {"success": True}


@router.delete("/high-priority-senders")
def delete_vip(
    payload: SenderEmailRuleBody,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    container.classification.remove_vip_sender(str(payload.sender_email))
    return {"success": True}


@router.get("/medium-priority-senders")
def list_medium(container: MailPulseContainer = Depends(get_mailpulse_container)):
    return {"medium_priority_senders": container.classification.load_medium_senders()}


@router.post("/medium-priority-senders")
def add_medium(
    payload: SenderEmailRuleBody,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    container.classification.save_medium_sender(str(payload.sender_email))
    return {"success": True}


@router.delete("/medium-priority-senders")
def delete_medium(
    payload: SenderEmailRuleBody,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    container.classification.remove_medium_sender(str(payload.sender_email))
    return {"success": True}
