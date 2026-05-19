"""Link multiple Gmail mailboxes to a Supabase-authenticated user."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.container import MailPulseContainer
from app.core.exceptions import AuthException
from app.core.logging import get_logger
from app.core.middleware.auth import get_supabase_user_id, require_supabase_user
from app.core.rate_limit import limiter
from app.deps import get_mailpulse_container
from app.services import email_cache, session_credentials

log = get_logger("gmail_accounts", service_name="GmailAccountsRoutes")

router = APIRouter(prefix="/api/gmail/accounts", dependencies=[Depends(require_supabase_user)])


class ActiveMailboxBody(BaseModel):
    mailbox: str = Field(..., min_length=3, max_length=320)


@router.get("")
def list_connected_accounts(request: Request, container: MailPulseContainer = Depends(get_mailpulse_container)):
    uid = get_supabase_user_id(request)
    rows = container.connected_accounts_repo.list_accounts(uid)
    active = session_credentials.get_active_mailbox(request.session)
    out: List[dict] = []
    for r in rows:
        em = str(r.get("account_email") or "")
        out.append(
            {
                "account_email": em,
                "updated_at": r.get("updated_at"),
                "is_active": bool(active and em.lower() == active.lower()),
            }
        )
    return {"accounts": out, "active_mailbox": active}


@router.post("/oauth/begin")
@limiter.limit("10/minute")
def begin_gmail_oauth(
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    if not uid:
        raise AuthException("Not authenticated", code="NOT_AUTHENTICATED", status_code=401)
    request.session["gmail_oauth_owner_sub"] = uid
    try:
        url = container.auth_service.build_authorization_url(request.session)
    except AuthException:
        raise
    except Exception as e:
        log.exception("gmail oauth begin failed (check GOOGLE_CREDENTIALS_PATH / client secrets JSON)")
        raise AuthException(
            "Could not start Google OAuth (see server logs)",
            code="OAUTH_BEGIN_FAILED",
            status_code=503,
        ) from e
    return {"authorization_url": url}


@router.post("/active")
def set_active_mailbox(
    body: ActiveMailboxBody,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    key = body.mailbox.strip().lower()
    rows = container.connected_accounts_repo.list_accounts(uid)
    allowed = {str(r.get("account_email") or "").lower() for r in rows}
    if key not in allowed:
        raise AuthException("Unknown mailbox for this user", code="UNKNOWN_MAILBOX", status_code=400)
    session_credentials.set_active_mailbox(request.session, key)
    return {"active_mailbox": key}


@router.delete("/{account_email:path}")
def disconnect_mailbox(
    account_email: str,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    key = account_email.strip().lower()
    if not key:
        raise AuthException("Invalid mailbox", code="INVALID_MAILBOX", status_code=400)
    try:
        container.gmail_sync.unregister_watch_for_user(key)
    except Exception:
        pass
    email_cache.clear(key)
    container.gmail_message_index_repo.delete_mailbox(uid, key)
    container.connected_accounts_repo.delete_account(uid, key)
    if session_credentials.get_active_mailbox(request.session) == key:
        request.session.pop("active_mailbox", None)
    return {"removed": key}
