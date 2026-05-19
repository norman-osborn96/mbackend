"""AI budget and CXO profile maintenance (authenticated)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.container import MailPulseContainer
from app.core.config import resolved_env_file_path
from app.core.exceptions import AIClassificationException, EmailSyncException, RateLimitException
from app.core.middleware.auth import require_supabase_user
from app.core.rate_limit import limiter
from app.deps import get_mailpulse_container
from app.services import email_cache, session_credentials
from app.services.ai_client import ALLOWED_PERSONA_FIELDS
from app.services.gmail_service import get_user_email

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(require_supabase_user)])


def _account_from_session(request: Request) -> str:
    creds = session_credentials.load_and_persist_fresh_for_request(request)
    try:
        return get_user_email(creds)
    except EmailSyncException as e:
        raise EmailSyncException(str(e), code="EMAIL_RESOLVE_FAILED") from e


class PersonaUpdate(BaseModel):
    designation: str = Field("", max_length=160)
    field: str = Field("", max_length=32)
    focus_notes: str = Field("", max_length=2000)


@router.get("/budget")
@limiter.limit("10/minute")
def ai_budget(
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    acct = _account_from_session(request)
    return container.ai_classifier.get_ai_budget(acct)


@router.get("/openrouter-status")
@limiter.limit("30/minute")
def openrouter_status(
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    """Whether an API key is loaded and which models are configured (no external call)."""
    s = container.settings
    key = s.openrouter_api_key
    raw = ""
    if key:
        raw = str(key.get_secret_value() or "").strip()
    key_suffix = raw[-4:] if len(raw) >= 4 else (raw if raw else None)
    return {
        "openrouter_configured": bool(raw),
        "primary_model": s.openrouter_primary_model,
        "fallback_model": s.openrouter_fallback_model,
        "openrouter_base_url": s.openrouter_base_url,
        "env_file_path": str(resolved_env_file_path()),
        "key_length_chars": len(raw),
        "key_suffix_last4": key_suffix,
        "hint": "If Activity is empty, the running app may be using a different key than your dashboard, or no request has succeeded (see POST /ai/openrouter-ping). Restart uvicorn after editing .env.",
    }


@router.post("/openrouter-ping")
@limiter.limit("5/minute")
def openrouter_ping(
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    """One tiny completion so you can confirm the key hits OpenRouter (Activity should show a row if ok)."""
    s = container.settings
    raw = ""
    if s.openrouter_api_key:
        raw = str(s.openrouter_api_key.get_secret_value() or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="OPENROUTER_API_KEY is empty in loaded settings")
    try:
        data = container.ai_client.chat_completion(
            messages=[
                {"role": "system", "content": "Reply with the single word OK and nothing else."},
                {"role": "user", "content": "ping"},
            ],
            max_tokens=8,
            temperature=0.0,
            model=s.openrouter_primary_model,
        )
        text = ""
        try:
            text = str(data.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
        except (IndexError, TypeError):
            pass
        return {
            "ok": True,
            "provider_reached": True,
            "model_used": s.openrouter_primary_model,
            "response_preview": text.strip()[:80],
            "hint": "Check OpenRouter Activity for this API key; last 4 chars should match GET /ai/openrouter-status key_suffix_last4.",
        }
    except RateLimitException as e:
        return {
            "ok": False,
            "provider_reached": True,
            "error": str(e),
            "error_code": "rate_limited",
            "model_tried": s.openrouter_primary_model,
            "hint": "OpenRouter accepted the key but returned 429. Activity may still be sparse; add credits or wait.",
        }
    except AIClassificationException as e:
        return {
            "ok": False,
            "provider_reached": True,
            "error": str(e),
            "error_code": getattr(e, "code", "AI_CLASSIFICATION_FAILED"),
            "details": getattr(e, "details", None),
            "model_tried": s.openrouter_primary_model,
            "hint": "HTTP error from OpenRouter (wrong model id, no access, etc.). Compare key_suffix_last4 with dashboard key.",
        }
    except Exception as e:
        return {
            "ok": False,
            "provider_reached": False,
            "error": str(e),
            "error_code": type(e).__name__,
            "hint": "Request may not have reached OpenRouter (network, DNS, firewall).",
        }


@router.post("/profile/refresh")
@limiter.limit("10/minute")
def refresh_ai_profile(
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    acct = _account_from_session(request)
    sample = (email_cache.list_all_messages(acct) or [])[:20]
    container.ai_classifier.maybe_warm_cxo_profile(acct, sample, force=True)
    return {"ok": True, "account_email": acct, "sample_size": len(sample)}


@router.get("/persona")
@limiter.limit("30/minute")
def get_ai_persona(
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    acct = _account_from_session(request)
    raw = container.sender_repository.get_user_ai_profile(acct) or {}
    ap = raw.get("admin_persona") if isinstance(raw.get("admin_persona"), dict) else {}
    return {
        "designation": str(ap.get("designation") or ""),
        "field": str(ap.get("field") or ""),
        "focus_notes": str(ap.get("focus_notes") or ""),
        "allowed_fields": sorted(ALLOWED_PERSONA_FIELDS),
    }


@router.put("/persona")
@limiter.limit("20/minute")
def put_ai_persona(
    request: Request,
    response: Response,
    body: PersonaUpdate,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    acct = _account_from_session(request)
    designation = (body.designation or "").strip()
    field = (body.field or "").strip().lower()
    focus_notes = (body.focus_notes or "").strip()
    if not designation:
        raise HTTPException(status_code=400, detail="designation is required")
    if field not in ALLOWED_PERSONA_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"field must be one of: {', '.join(sorted(ALLOWED_PERSONA_FIELDS))}",
        )
    existing = container.sender_repository.get_user_ai_profile(acct)
    if not isinstance(existing, dict):
        existing = {}
    existing = dict(existing)
    existing["admin_persona"] = {
        "designation": designation[:160],
        "field": field,
        "focus_notes": focus_notes[:2000],
    }
    container.sender_repository.upsert_user_ai_profile(acct, existing)
    container.ai_classifier.invalidate_profile_cache(acct)
    return {"ok": True, "account_email": acct}
