from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from app.services.priority_engine import (
    load_vip_senders,
    save_vip_sender,
    remove_vip_sender,
    load_medium_senders,
    save_medium_sender,
    remove_medium_sender,
)

router = APIRouter(prefix="/priority", tags=["priority"])


class SenderPayload(BaseModel):
    email: EmailStr


@router.get("/vip")
def get_vip_senders_api():
    try:
        data = load_vip_senders()
        return {"success": True, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/vip")
def add_vip_sender_api(payload: SenderPayload):
    try:
        save_vip_sender(payload.email)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/vip")
def delete_vip_sender_api(payload: SenderPayload):
    try:
        remove_vip_sender(payload.email)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/medium")
def get_medium_senders_api():
    try:
        data = load_medium_senders()
        return {"success": True, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/medium")
def add_medium_sender_api(payload: SenderPayload):
    try:
        save_medium_sender(payload.email)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/medium")
def delete_medium_sender_api(payload: SenderPayload):
    try:
        remove_medium_sender(payload.email)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))