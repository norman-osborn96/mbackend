"""Google OAuth login, callback, profile fetch, and logout."""

from __future__ import annotations

import traceback
from typing import Any, Callable, Dict, MutableMapping, Optional

import requests
from fastapi import BackgroundTasks
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow

from app.core.config import Settings
from app.core.exceptions import AuthException
from app.core.logging import get_logger
from app.repositories.connected_accounts_repository import ConnectedAccountsRepository
from app.services import session_credentials

log = get_logger("auth_service", service_name="AuthService")


def _google_primary_email_from_access_token(settings: Settings, access_token: str) -> str:
    if not access_token:
        return ""
    resp = requests.get(
        settings.google_userinfo_url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=8,
    )
    if resp.status_code != 200:
        return ""
    data = resp.json()
    return str(data.get("email") or "").strip().lower()


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _get_flow(self) -> Flow:
        path = self._settings.google_credentials_path
        if not path.exists():
            raise AuthException(
                "Missing Google OAuth client secrets file",
                code="MISSING_GOOGLE_CREDENTIALS",
                status_code=500,
            )
        return Flow.from_client_secrets_file(
            str(path),
            scopes=self._settings.oauth_scope_list(),
            redirect_uri=self._settings.oauth_redirect_uri,
        )

    def build_authorization_url(self, session: MutableMapping[str, Any]) -> str:
        flow = self._get_flow()
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        session["state"] = state
        session["code_verifier"] = flow.code_verifier
        return authorization_url

    def build_login_redirect(self, session: MutableMapping[str, Any]) -> RedirectResponse:
        return RedirectResponse(self.build_authorization_url(session))

    def handle_oauth_callback(
        self,
        session: MutableMapping[str, Any],
        request_url: str,
        background_tasks: BackgroundTasks,
        watch_callback: Callable[..., Any],
        connected_accounts_repo: Optional[ConnectedAccountsRepository] = None,
    ) -> RedirectResponse:
        state = session.get("state")
        if not state:
            return RedirectResponse(f"{self._settings.frontend_url}/login?error=invalid_state")

        flow = self._get_flow()
        code_verifier = session.get("code_verifier")
        if code_verifier:
            flow.code_verifier = code_verifier

        try:
            flow.fetch_token(authorization_response=request_url)
        except Exception as e:
            log.exception("OAuth token exchange failed: %s", e)
            try:
                self._settings.auth_error_log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._settings.auth_error_log_path, "w", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            except OSError as w:
                log.warning("could not write auth error log: %s", w)
            return RedirectResponse(f"{self._settings.frontend_url}/login?error=auth_failed")

        creds = flow.credentials
        scopes_list = list(creds.scopes) if creds.scopes else self._settings.oauth_scope_list()

        creds_dict = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": scopes_list,
        }

        link_owner = session.get("gmail_oauth_owner_sub")
        owner_for_watch: Optional[str] = None
        session_credentials.store_google_credentials(session, creds_dict)

        if link_owner and connected_accounts_repo:
            mapped_email = _google_primary_email_from_access_token(self._settings, creds_dict.get("token") or "")
            if mapped_email:
                try:
                    connected_accounts_repo.upsert_account(link_owner, mapped_email, creds_dict)
                    session["active_mailbox"] = mapped_email
                    owner_for_watch = link_owner
                except Exception as ex:
                    log.exception("connected_gmail_accounts upsert failed: %s", ex)
            else:
                log.warning("skipped connected_gmail_accounts upsert: empty email from Google")
        session.pop("gmail_oauth_owner_sub", None)

        background_tasks.add_task(watch_callback, creds_dict, owner_for_watch)
        return RedirectResponse(f"{self._settings.frontend_url}/")

    def fetch_user_profile(self, session: MutableMapping[str, Any]) -> Dict[str, str]:
        creds = session_credentials.get_google_credentials(session)
        if not creds:
            raise AuthException("Not authenticated", code="NOT_AUTHENTICATED", status_code=401)

        cached = session.get("user_profile")
        if cached:
            return cached

        try:
            resp = requests.get(
                self._settings.google_userinfo_url,
                headers={"Authorization": f"Bearer {creds['token']}"},
                timeout=5,
            )
        except requests.RequestException as e:
            raise AuthException(
                "Failed to reach Google userinfo",
                code="USERINFO_UNAVAILABLE",
                status_code=502,
            ) from e

        if resp.status_code != 200:
            raise AuthException(
                "Failed to fetch user profile",
                code="USERINFO_REJECTED",
                status_code=resp.status_code,
            )

        data = resp.json()
        profile = {
            "email": data.get("email", ""),
            "name": data.get("name", ""),
            "given_name": data.get("given_name", ""),
            "picture": data.get("picture", ""),
        }
        session["user_profile"] = profile
        return profile

    def clear_session(self, session: MutableMapping[str, Any]) -> None:
        session.clear()
