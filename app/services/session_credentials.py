"""Encrypted Google OAuth credentials: server session and/or Supabase per-user store."""

from __future__ import annotations

from typing import Any, MutableMapping, Optional

from fastapi import Request

from app.core.exceptions import AuthException
from app.services import gmail_service, oauth_token_crypto

_SESSION_KEY = "google_oauth_enc"
_LEGACY_KEY = "credentials"


def store_google_credentials(session: MutableMapping[str, Any], creds: dict[str, Any]) -> None:
    """Persist Gmail OAuth dict under an encrypted session key; removes legacy plaintext."""
    session[_SESSION_KEY] = oauth_token_crypto.encrypt_credentials_blob(creds)
    session.pop(_LEGACY_KEY, None)


def get_google_credentials(session: MutableMapping[str, Any]) -> Optional[dict[str, Any]]:
    """Return decrypted Google OAuth credentials, or None if absent."""
    if _SESSION_KEY in session:
        return oauth_token_crypto.decrypt_credentials_blob(session[_SESSION_KEY])
    legacy = session.get(_LEGACY_KEY)
    if isinstance(legacy, dict):
        return legacy
    return None


def require_google_credentials(session: MutableMapping[str, Any]) -> dict[str, Any]:
    creds = get_google_credentials(session)
    if not creds:
        raise AuthException("Not authenticated with Google", code="NO_GOOGLE_CREDENTIALS", status_code=401)
    return creds


def clear_google_credentials(session: MutableMapping[str, Any]) -> None:
    session.pop(_SESSION_KEY, None)
    session.pop(_LEGACY_KEY, None)


def get_active_mailbox(session: MutableMapping[str, Any]) -> Optional[str]:
    v = session.get("active_mailbox")
    if isinstance(v, str) and v.strip():
        return v.strip().lower()
    return None


def set_active_mailbox(session: MutableMapping[str, Any], mailbox: str) -> None:
    m = (mailbox or "").strip().lower()
    if m:
        session["active_mailbox"] = m


def resolve_request_mailbox(request: Request) -> Optional[str]:
    h = request.headers.get("X-MailPulse-Mailbox") or request.headers.get("x-mailpulse-mailbox")
    if isinstance(h, str) and h.strip():
        return h.strip().lower()
    sess = get_active_mailbox(request.session)
    if sess:
        return sess
    uid = str(getattr(request.state, "supabase_user_id", "") or "")
    if uid:
        try:
            from app.container import get_container

            rows = get_container().connected_accounts_repo.list_accounts(uid)
            if len(rows) == 1:
                return str(rows[0].get("account_email") or "").strip().lower()
        except Exception:
            pass
    return None


def load_and_persist_fresh_for_request(request: Request) -> dict[str, Any]:
    """Resolve Gmail creds from Supabase (per mailbox) or legacy session cookie; refresh if needed."""
    session = request.session
    user_id = str(getattr(request.state, "supabase_user_id", "") or "")
    mailbox = resolve_request_mailbox(request)
    creds: Optional[dict[str, Any]] = None
    if user_id and mailbox:
        try:
            from app.container import get_container

            creds = get_container().connected_accounts_repo.get_decrypted_credentials(user_id, mailbox)
        except Exception:
            creds = None
    if not creds:
        creds = get_google_credentials(session)
    if not creds:
        raise AuthException("Not authenticated with Google", code="NO_GOOGLE_CREDENTIALS", status_code=401)
    if gmail_service.ensure_google_credentials_fresh(creds):
        if user_id and mailbox:
            try:
                from app.container import get_container

                get_container().connected_accounts_repo.upsert_account(user_id, mailbox, creds)
            except Exception:
                store_google_credentials(session, creds)
        else:
            store_google_credentials(session, creds)
    return creds
