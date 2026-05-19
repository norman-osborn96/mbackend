"""Rate limiting (slowapi)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _rate_limit_key(request: Request) -> str:
    uid = getattr(request.state, "supabase_user_id", None)
    if uid:
        return f"user:{uid}"
    return f"ip:{get_remote_address(request)}"


# No global default: slowapi middleware applies default_limits to *every* request before
# per-route limits; a shared 60/min cap starves dashboards (many GETs) and blocks POSTs like
# suggest-reply. Abuse boundaries remain on routes that declare @limiter.limit(...).
limiter = Limiter(
    key_func=_rate_limit_key,
    default_limits=[],
    headers_enabled=True,
)


def register_rate_limit_exemptions(app: FastAPI) -> None:
    """Mark /health, /ready, and Gmail Pub/Sub webhook as exempt (slowapi uses endpoint names)."""
    lim = app.state.limiter
    exempt_paths = frozenset({"/health", "/ready", "/gmail/webhook"})
    for route in app.routes:
        path = getattr(route, "path", None)
        if path not in exempt_paths:
            continue
        ep = getattr(route, "endpoint", None)
        if ep is None:
            continue
        lim._exempt_routes.add(f"{ep.__module__}.{ep.__name__}")
