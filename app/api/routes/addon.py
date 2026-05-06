from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
import os
from app.services.ai_classifier import classify_email_ai, summarize_email, generate_reply_suggestion
from app.core.logger import get_logger

router = APIRouter(prefix="/addon", tags=["addon"])
log = get_logger("addon_routes")

# Simple API Key for Add-on authentication
ADDON_API_KEY = os.getenv("ADDON_API_KEY", "mailpulse-addon-secret-key")

class AnalyzeRequest(BaseModel):
    subject: str
    snippet: str
    sender: str = ""

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
        # Run classification
        classification = classify_email_ai(body.subject, body.snippet)
        
        # Run summary
        summary = summarize_email(body.subject, body.snippet)
        
        # Run reply suggestion
        suggestion = generate_reply_suggestion(body.subject, body.snippet, body.sender)
        
        return {
            "priority": classification.get("level", "LOW"),
            "reason": classification.get("reason", ""),
            "summary": summary,
            "suggestion": suggestion
        }
    except Exception as e:
        log.error("Add-on analysis failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
