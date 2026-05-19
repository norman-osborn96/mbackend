"""Liveness and readiness probes (unauthenticated, not rate-limited)."""

from __future__ import annotations

import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    settings = get_settings()
    return {"status": "ok", "version": settings.mailpulse_version}


@router.get("/ready")
def ready():
    settings = get_settings()
    failures: list[str] = []

    try:
        r = requests.get(
            f"{settings.supabase_url.rstrip('/')}/rest/v1/",
            headers={
                "apikey": settings.supabase_key.get_secret_value(),
                "Authorization": f"Bearer {settings.supabase_key.get_secret_value()}",
            },
            timeout=5,
        )
        if r.status_code >= 500:
            failures.append("supabase")
    except requests.RequestException:
        failures.append("supabase")

    try:
        g = requests.get("https://www.googleapis.com/generate_204", timeout=5)
        if g.status_code not in (204, 200):
            failures.append("google_reachability")
    except requests.RequestException:
        failures.append("google_reachability")

    if failures:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "failures": failures},
        )
    return {"status": "ready", "version": settings.mailpulse_version}


@router.get("/auth/login")
def legacy_auth_login_alias():
    return RedirectResponse(url="/api/auth/login", status_code=307)
