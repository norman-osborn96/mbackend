"""Supabase JWT validation (middleware + optional dependency)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, FrozenSet

import jwt
from fastapi import Request
from jwt import PyJWKClient, PyJWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from app.core.config import get_settings
from app.core.supabase_user_postgrest import build_user_postgrest_bridge
from app.core.supabase_request_context import MAILPULSE_USER_SUPABASE_CLIENT_STATE_ATTR
from app.core.logging import request_id_var

# Paths that skip Supabase JWT (exact or glob-style with single *)
PUBLIC_JWT_PATHS: FrozenSet[str] = frozenset(
    {
        "/health",
        "/ready",
        "/",
        "/api/auth/login",
        "/api/auth/callback",
        # Session-cookie Gmail OAuth; must work before any Supabase session exists
        "/api/auth/status",
        "/api/auth/me",
        "/api/auth/logout",
        "/auth/login",
        "/auth/callback",
        "/gmail/webhook",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)


def path_is_public_for_jwt(path: str) -> bool:
    if path in PUBLIC_JWT_PATHS:
        return True
    if path.startswith("/docs/") or path.startswith("/redoc/"):
        return True
    return False


_ASYM_JWT_ALGS: FrozenSet[str] = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
)


@lru_cache(maxsize=8)
def _jwks_client(jwks_url: str) -> PyJWKClient:
    """Fetch JWKS from Supabase Auth (required when access tokens use asymmetric signing keys)."""
    return PyJWKClient(jwks_url, cache_keys=True)


def _structured_401(message: str, code: str = "INVALID_TOKEN") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id_var.get("-"),
            }
        },
    )


def verify_supabase_jwt_from_request(request: Request, *, bearer_token: str | None = None) -> dict[str, Any]:
    """Validate Supabase access JWT and return claims.

    Pass ``bearer_token`` when the caller already extracted it (must match ``Authorization``).
    """
    settings = get_settings()
    if bearer_token is not None:
        token = bearer_token.strip()
        if not token:
            raise ValueError("Empty bearer token")
    else:
        auth = request.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            raise ValueError("Missing or invalid Authorization header")
        token = auth[7:].strip()
        if not token:
            raise ValueError("Empty bearer token")

    header = jwt.get_unverified_header(token)
    alg = str(header.get("alg") or "HS256").upper()
    audience = settings.supabase_jwt_audience
    decode_opts: dict[str, Any] = {"require": ["exp", "sub"]}

    try:
        if alg == "HS256":
            secret = settings.supabase_jwt_secret.get_secret_value().strip()
            if not secret:
                raise ValueError("SUPABASE_JWT_SECRET is empty")
            payload = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=audience,
                options=decode_opts,
            )
        elif alg in _ASYM_JWT_ALGS:
            issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1"
            jwks_url = f"{issuer}/.well-known/jwks.json"
            signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[alg],
                audience=audience,
                issuer=issuer,
                options=decode_opts,
            )
        else:
            raise ValueError(f"Unsupported JWT algorithm: {alg}")
    except jwt.ExpiredSignatureError as e:
        raise ValueError("Token expired") from e
    except PyJWTError as e:
        raise ValueError(
            "Invalid token — HS256: copy the legacy JWT secret into SUPABASE_JWT_SECRET "
            "(Dashboard → Project Settings → API; strip spaces; same project as SUPABASE_URL). "
            "RS256 / signing keys: SUPABASE_URL must match that project; verification uses JWKS."
        ) from e
    return payload


def apply_supabase_claims_to_state(request: Request, payload: dict[str, Any]) -> None:
    request.state.supabase_jwt = payload
    request.state.supabase_user_id = str(payload.get("sub") or "")
    request.state.supabase_user_email = str(payload.get("email") or "")


class SupabaseJWTAuthMiddleware(BaseHTTPMiddleware):
    """Require a valid Supabase JWT on all non-public routes."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path_is_public_for_jwt(path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization") or ""
        bearer = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""

        settings = get_settings()
        setattr(request.state, MAILPULSE_USER_SUPABASE_CLIENT_STATE_ATTR, None)

        try:
            payload = verify_supabase_jwt_from_request(request, bearer_token=bearer if bearer else None)
        except ValueError as e:
            msg = str(e)
            code = "TOKEN_EXPIRED" if "expired" in msg.lower() else "INVALID_TOKEN"
            return _structured_401(msg, code=code)

        anon = settings.supabase_anon_key
        if bearer and anon is not None:
            setattr(
                request.state,
                MAILPULSE_USER_SUPABASE_CLIENT_STATE_ATTR,
                build_user_postgrest_bridge(
                    settings.supabase_url,
                    anon.get_secret_value(),
                    bearer,
                ),
            )

        apply_supabase_claims_to_state(request, payload)
        try:
            return await call_next(request)
        finally:
            setattr(request.state, MAILPULSE_USER_SUPABASE_CLIENT_STATE_ATTR, None)


def require_supabase_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency: ensure JWT middleware attached claims (defense in depth)."""
    payload = getattr(request.state, "supabase_jwt", None)
    if not payload or not getattr(request.state, "supabase_user_id", None):
        from app.core.exceptions import AuthException

        raise AuthException(
            "Authentication required",
            code="NOT_AUTHENTICATED",
            status_code=401,
        )
    return payload


def get_supabase_user_id(request: Request) -> str:
    return str(getattr(request.state, "supabase_user_id", "") or "")


def get_supabase_user_email(request: Request) -> str:
    return str(getattr(request.state, "supabase_user_email", "") or "")
