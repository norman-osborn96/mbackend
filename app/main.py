import os
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # Allow HTTP for local dev

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from app.api.routes import dashboard, emails, rules, priority, auth, gmail, followups
from app.core import scheduler, followup_scheduler
from app.core.logger import get_logger

log = get_logger("main")

app = FastAPI(title="MailPulse API")

# CORS must list specific origins when using credentials (cookies)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "https://mailpulse.netlify.app",
        "https://mail-pulse.netlify.app",
        "https://pulse-mail.netlify.app"
    ],

    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    SessionMiddleware,
    secret_key="some-super-secret-mailpulse-key",
    max_age=3600 * 24 * 7,  # 7 days
    same_site="none",
    https_only=True,
)

app.include_router(dashboard.router)
app.include_router(emails.router)
app.include_router(rules.router)
app.include_router(priority.router)
app.include_router(auth.router)
app.include_router(gmail.router)
app.include_router(followups.router)


@app.on_event("startup")
def _on_startup() -> None:
    log.info("MailPulse starting up | push-based Gmail pipeline enabled")
    scheduler.start()
    followup_scheduler.start()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    log.info("MailPulse shutting down")
    scheduler.stop()
    followup_scheduler.stop()


@app.get("/")
def root():
    return {"message": "Email Priority API running"}
