from fastapi import APIRouter
from pydantic import BaseModel
from app.services.priority_engine import (
    save_vip_sender,
    remove_vip_sender,
    load_vip_senders,
    save_medium_sender,
    remove_medium_sender,
    load_medium_senders,
)

router = APIRouter(prefix="/rules", tags=["rules"])

class SenderRuleRequest(BaseModel):
    sender_email: str

@router.get("/high-priority-senders")
def list_vip():
    return {"high_priority_senders": load_vip_senders()}

@router.post("/high-priority-senders")
def add_vip(payload: SenderRuleRequest):
    save_vip_sender(payload.sender_email)
    return {"success": True}

@router.delete("/high-priority-senders")
def delete_vip(payload: SenderRuleRequest):
    remove_vip_sender(payload.sender_email)
    return {"success": True}


# --- MEDIUM ---

@router.get("/medium-priority-senders")
def list_medium():
    return {"medium_priority_senders": load_medium_senders()}


@router.post("/medium-priority-senders")
def add_medium(payload: SenderRuleRequest):
    save_medium_sender(payload.sender_email)
    return {"success": True}


@router.delete("/medium-priority-senders")
def delete_medium(payload: SenderRuleRequest):
    remove_medium_sender(payload.sender_email)
    return {"success": True}