from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from app.container import MailPulseContainer
from app.core.rate_limit import limiter
from app.deps import get_mailpulse_container
from app.services import session_credentials

public_auth_router = APIRouter(prefix="/api/auth")


@public_auth_router.get("/login")
@limiter.limit("5/minute")
def login(
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    return container.auth_service.build_login_redirect(request.session)


def _auto_start_gmail_watch(creds_dict: dict, owner_user_id: Optional[str] = None) -> None:
    from app.container import get_container
    from app.core.logging import get_logger

    log = get_logger("auth_watch", service_name="AuthService")
    try:
        get_container().gmail_sync.ensure_watch_for_user(creds_dict, owner_user_id=owner_user_id)
    except Exception as e:
        log.error("automatic Gmail watch setup failed: %s", e)


@public_auth_router.get("/callback")
@limiter.limit("5/minute")
def callback(
    request: Request,
    background_tasks: BackgroundTasks,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    return container.auth_service.handle_oauth_callback(
        request.session,
        str(request.url),
        background_tasks,
        watch_callback=_auto_start_gmail_watch,
        connected_accounts_repo=container.connected_accounts_repo,
    )


@public_auth_router.get("/status")
def status(request: Request):
    creds = session_credentials.get_google_credentials(request.session)
    return {"authenticated": bool(creds)}


@public_auth_router.get("/me")
def me(
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    return container.auth_service.fetch_user_profile(request.session)


@public_auth_router.post("/logout")
@limiter.limit("5/minute")
def logout(
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    container.auth_service.clear_session(request.session)
    return {"message": "Logged out"}
