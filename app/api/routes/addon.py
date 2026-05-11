from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
import os
from app.services.priority_engine import calculate_priority, save_vip_sender, save_medium_sender
from app.services.ai_classifier import generate_reply_suggestion
from app.core.logger import get_logger

router = APIRouter(prefix="/addon", tags=["addon"])
log = get_logger("addon_routes")

# Simple API Key for Add-on authentication
ADDON_API_KEY = os.getenv("ADDON_API_KEY", "mailpulse-addon-secret-key")

class AnalyzeRequest(BaseModel):
    subject: str
    snippet: str
    sender: str = ""
    tone: str = "professional"

class PriorityUpdateRequest(BaseModel):
    email: str
    level: str

@router.post("/analyze")
async def analyze_email(
    body: AnalyzeRequest, 
    x_api_key: str = Header(None)
):
    if x_api_key != ADDON_API_KEY:
        log.warning("Add-on access denied: Invalid API Key")
        raise HTTPException(status_code=401, detail="Invalid API Key")
    
    log.info("Add-on analysis requested for: %s", body.subject[:50])
    
    try:
        # Use the full priority engine
        email_data = {
            "subject": body.subject,
            "snippet": body.snippet,
            "sender": body.sender
        }
        result = calculate_priority(email_data)
        
        # Add reply suggestion
        suggestion = generate_reply_suggestion(body.subject, body.snippet, body.sender, body.tone)
        result["suggestion"] = suggestion
        
        # Map for backward compatibility if needed, but we'll update the addon too
        result["priority"] = result.get("level", "LOW")
        # Combine reasons into a single string for simple display if needed
        result["reason"] = "; ".join(result.get("reasons", []))
        
        return result
    except Exception as e:
        log.error("Add-on analysis failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/set-priority")
async def set_sender_priority(
    body: PriorityUpdateRequest, 
    x_api_key: str = Header(None)
):
    if x_api_key != ADDON_API_KEY:
        log.warning("Add-on priority update denied: Invalid API Key")
        raise HTTPException(status_code=401, detail="Invalid API Key")
    
    log.info("Add-on setting priority %s for %s", body.level, body.email)
    
    try:
        success = False
        if body.level == "HIGH":
            success = save_vip_sender(body.email)
        elif body.level == "MEDIUM":
            success = save_medium_sender(body.email)
        
        return {"success": success}
    except Exception as e:
        log.error("Add-on priority update failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
