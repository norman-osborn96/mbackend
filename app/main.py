import os
from contextlib import asynccontextmanager

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.api.routes import ai, auth, bucket_rules, buckets as buckets_router, dashboard, emails, followups, gmail, gmail_accounts, health, priority, rules
from app.container import MailPulseContainer, set_container
from app.core import followup_scheduler, scheduler
from app.core.config import get_settings
from app.core.error_handlers import (
    http_exception_handler,
    mailpulse_exception_handler,
    rate_limit_exception_handler,
    validation_exception_handler,
)
from app.core.exceptions import MailPulseBaseException
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestIdMiddleware
from app.core.middleware.auth import SupabaseJWTAuthMiddleware
from app.core.middleware.security import SecurityHeadersMiddleware
from app.core.rate_limit import limiter, register_rate_limit_exemptions
from app.services.oauth_token_crypto import init_fernet

log = get_logger("main", service_name="MailPulse")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.mailpulse_log_level)
    if settings.oauthlib_insecure_transport:
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    init_fernet(settings.oauth_token_fernet_key.get_secret_value())

    container = MailPulseContainer.build(settings)
    app.state.container = container
    set_container(container)

    log.info("MailPulse starting up | push-based Gmail pipeline enabled")
    scheduler.start()
    followup_scheduler.start()
    yield
    log.info("MailPulse shutting down")
    scheduler.stop()
    followup_scheduler.stop()
    container.gmail_sync.shutdown()
    await container.sender_repository.aclose()


app = FastAPI(title="MailPulse API", lifespan=lifespan)
app.state.limiter = limiter

_settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.session_secret.get_secret_value(),
    max_age=_settings.session_max_age_seconds,
    same_site=_settings.session_same_site,
    https_only=_settings.session_https_only,
)

app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SupabaseJWTAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list(),
    allow_credentials=True,
    allow_methods=_settings.cors_allow_methods(),
    allow_headers=_settings.cors_allow_headers(),
)

app.include_router(health.router)
app.include_router(auth.public_auth_router)
app.include_router(dashboard.router)
app.include_router(emails.router)
app.include_router(buckets_router.router)
app.include_router(bucket_rules.router)
app.include_router(rules.router)
app.include_router(priority.router)
app.include_router(gmail.public_gmail_router)
app.include_router(gmail.protected_gmail_router)
app.include_router(gmail_accounts.router)
app.include_router(followups.router)
app.include_router(ai.router)

app.add_exception_handler(MailPulseBaseException, mailpulse_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)


@app.get("/")
def root():
    return {"message": "Email Priority API running"}


register_rate_limit_exemptions(app)
