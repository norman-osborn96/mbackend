"""Fernet encryption for OAuth token blobs at rest (session + watch_state)."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.exceptions import AuthException
from app.core.logging import get_logger

log = get_logger("oauth_token_crypto", service_name="OAuthTokenCrypto")

_fernet: Optional[Fernet] = None


def init_fernet(key_b64: str) -> None:
    """Initialize process-wide Fernet from a urlsafe base64-encoded 32-byte key."""
    global _fernet
    _fernet = Fernet(key_b64.encode("ascii"))


def is_configured() -> bool:
    return _fernet is not None


def encrypt_credentials_blob(data: Dict[str, Any]) -> str:
    if _fernet is None:
        raise RuntimeError("OAuth token crypto not initialized")
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _fernet.encrypt(raw).decode("ascii")


def decrypt_credentials_blob(token: str) -> Dict[str, Any]:
    if _fernet is None:
        raise RuntimeError("OAuth token crypto not initialized")
    try:
        raw = _fernet.decrypt(token.encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
    except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("Failed to decrypt OAuth blob")
        raise AuthException(
            "Stored OAuth credentials are invalid or corrupted",
            code="OAUTH_DECRYPT_FAILED",
            status_code=401,
        ) from e
    if not isinstance(obj, dict):
        raise AuthException("Invalid OAuth credential payload", code="OAUTH_INVALID_PAYLOAD", status_code=401)
    return obj
