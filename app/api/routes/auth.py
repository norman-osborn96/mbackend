from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse
import os
from google_auth_oauthlib.flow import Flow
from app.core.logger import get_logger
from app.services import gmail_sync
from app.api.routes.auth import router as auth_router

app.include_router(auth_router)

router = APIRouter(prefix="/api/auth")
log = get_logger("auth_route")

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
]

# Use dynamic or Render URL for callback
REDIRECT_URI = os.getenv("REDIRECT_URI", "https://mbackend-eq1g.onrender.com/api/auth/callback")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://mailpulse.netlify.app")

def get_flow():
    if not os.path.exists("app/credentials.json"):
        raise HTTPException(status_code=500, detail="Missing Google credentials.json")
    
    flow = Flow.from_client_secrets_file(
        "app/credentials.json", 
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    return flow

@router.get("/login")
def login(request: Request):
    flow = get_flow()
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    request.session["state"] = state
    # Save PKCE code_verifier to session (google-auth generates one automatically)
    request.session["code_verifier"] = flow.code_verifier
    return RedirectResponse(authorization_url)

def _auto_start_gmail_watch(creds_dict: dict) -> None:
    """Start Gmail push notifications after login without blocking redirect."""
    try:
        gmail_sync.ensure_watch_for_user(creds_dict)
    except Exception as e:
        # Do not fail OAuth login if Pub/Sub is not configured yet. The /emails
        # endpoint will also try lazy initialization on the next inbox request.
        log.error("automatic Gmail watch setup failed: %s", e)


@router.get("/callback")
def callback(request: Request, background_tasks: BackgroundTasks):
    try:
        state = request.session.get("state")
        if not state:
            return RedirectResponse(f"{FRONTEND_URL}/login?error=invalid_state")

        flow = get_flow()
        
        # Restore the PKCE code verifier from the session
        code_verifier = request.session.get("code_verifier")
        if code_verifier:
            flow.code_verifier = code_verifier

        # Use the full callback URL to exchange the auth code for tokens
        flow.fetch_token(authorization_response=str(request.url))
        
        creds = flow.credentials
        
        # Convert scopes frozenset to list for JSON serialization
        scopes_list = list(creds.scopes) if creds.scopes else list(SCOPES)

        # Store credentials in the session
        creds_dict = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": scopes_list
        }
        request.session["credentials"] = creds_dict
        background_tasks.add_task(_auto_start_gmail_watch, creds_dict)
        
        # Redirect back to the React app
        return RedirectResponse(f"{FRONTEND_URL}/")
    except Exception as e:
        import traceback
        print("Auth Callback Error:", e)
        traceback.print_exc()
        try:
            with open("auth_error.log", "w") as f:
                f.write(traceback.format_exc())
        except:
            pass
        return RedirectResponse(f"{FRONTEND_URL}/login?error=auth_failed")

@router.get("/status")
def status(request: Request):
    creds = request.session.get("credentials")
    if creds:
        return {"authenticated": True}
    return {"authenticated": False}


@router.get("/me")
def me(request: Request):
    """Return the authenticated Google user's profile (email, name, picture)."""
    creds = request.session.get("credentials")
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Return cached profile if already stored in session
    profile = request.session.get("user_profile")
    if profile:
        return profile

    # Fetch from Google's userinfo endpoint
    try:
        import requests as http_requests
        resp = http_requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {creds['token']}"},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            profile = {
                "email": data.get("email", ""),
                "name": data.get("name", ""),
                "given_name": data.get("given_name", ""),
                "picture": data.get("picture", ""),
            }
            request.session["user_profile"] = profile
            return profile
        else:
            raise HTTPException(status_code=resp.status_code, detail="Failed to fetch user profile")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile fetch error: {str(e)}")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"message": "Logged out"}
