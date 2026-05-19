"""Application settings loaded from environment (Pydantic BaseSettings)."""

from __future__ import annotations

from pathlib import Path
from typing import List

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_credentials_path() -> Path:
    return Path(__file__).resolve().parent.parent / "credentials.json"


# Resolve backend/.env regardless of process cwd (fixes missing SUPABASE_KEY when uvicorn runs elsewhere).
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ENV_FILE = _BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    """All configuration is validated at process startup."""

    model_config = SettingsConfigDict(
        env_file=_DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    supabase_url: str = Field(validation_alias="SUPABASE_URL")
    supabase_key: SecretStr = Field(
        validation_alias="SUPABASE_KEY",
        description="Primary Supabase key — prefer service_role for server repos.",
    )
    supabase_anon_key: SecretStr | None = Field(
        default=None,
        validation_alias="SUPABASE_ANON_KEY",
        description="Anon key + caller JWT for bucket RLS when service_role bypass fails.",
    )
    supabase_jwt_secret: SecretStr = Field(validation_alias="SUPABASE_JWT_SECRET")
    supabase_jwt_audience: str = Field(
        default="authenticated",
        validation_alias="SUPABASE_JWT_AUDIENCE",
    )

    oauth_token_fernet_key: SecretStr = Field(validation_alias="OAUTH_TOKEN_FERNET_KEY")

    mailpulse_version: str = Field(default="0.1.0", validation_alias="MAILPULSE_VERSION")

    openrouter_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENROUTER_API_KEY",
    )
    openrouter_model: str = Field(
        default="meta-llama/llama-3.2-3b-instruct:free",
        validation_alias="OPENROUTER_MODEL",
    )
    openrouter_primary_model: str = Field(
        default="meta-llama/llama-3.2-3b-instruct:free",
        validation_alias="OPENROUTER_PRIMARY_MODEL",
    )
    openrouter_fallback_model: str = Field(
        default="google/gemma-4-26b-a4b-it:free",
        validation_alias="OPENROUTER_FALLBACK_MODEL",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1/chat/completions",
        validation_alias="OPENROUTER_BASE_URL",
    )
    openrouter_http_referer: str = Field(
        default="http://localhost",
        validation_alias="OPENROUTER_HTTP_REFERER",
    )
    openrouter_app_title: str = Field(
        default="MailPulse",
        validation_alias="OPENROUTER_APP_TITLE",
    )
    openrouter_timeout_seconds: float = Field(
        default=10.0,
        validation_alias="OPENROUTER_TIMEOUT_SECONDS",
    )

    gmail_pubsub_topic: str = Field(validation_alias="GMAIL_PUBSUB_TOPIC")

    google_credentials_path: Path = Field(
        default_factory=lambda: _default_credentials_path(),
        validation_alias="GOOGLE_CREDENTIALS_PATH",
    )
    oauth_redirect_uri: str = Field(
        default="http://localhost:8000/api/auth/callback",
        validation_alias="OAUTH_REDIRECT_URI",
    )
    oauth_scopes: str = Field(
        default=(
            "https://www.googleapis.com/auth/gmail.readonly,"
            "https://www.googleapis.com/auth/gmail.send,"
            "openid,"
            "https://www.googleapis.com/auth/userinfo.email"
        ),
        validation_alias="OAUTH_SCOPES",
    )

    frontend_url: str = Field(
        default="http://localhost:3000",
        validation_alias="FRONTEND_URL",
    )
    cors_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
        validation_alias="CORS_ORIGINS",
    )
    session_secret: SecretStr = Field(validation_alias="SESSION_SECRET")
    session_max_age_seconds: int = Field(
        default=3600 * 24 * 7,
        validation_alias="SESSION_MAX_AGE_SECONDS",
    )
    session_https_only: bool = Field(
        default=False,
        validation_alias="SESSION_HTTPS_ONLY",
    )
    session_same_site: str = Field(default="lax", validation_alias="SESSION_SAME_SITE")

    oauthlib_insecure_transport: bool = Field(
        default=True,
        validation_alias="OAUTHLIB_INSECURE_TRANSPORT",
    )

    mailpulse_log_level: str = Field(default="INFO", validation_alias="MAILPULSE_LOG_LEVEL")
    mailpulse_fallback_scheduler_enabled: bool = Field(
        default=True,
        validation_alias="MAILPULSE_FALLBACK_SCHEDULER_ENABLED",
        description="Background Gmail safety sync for all mailboxes in watch-state (disable locally to reduce log noise).",
    )
    mailpulse_fallback_sync_seconds: int = Field(
        default=12 * 60,
        validation_alias="MAILPULSE_FALLBACK_SYNC_SECONDS",
    )
    mailpulse_followup_seconds: int = Field(
        default=10 * 60,
        validation_alias="MAILPULSE_FOLLOWUP_SECONDS",
    )
    gmail_bootstrap_max_messages: int = Field(
        default=5000,
        validation_alias="GMAIL_BOOTSTRAP_MAX_MESSAGES",
        description="Maximum messages imported by an async mailbox bootstrap.",
    )
    gmail_sync_workers: int = Field(
        default=4,
        validation_alias="GMAIL_SYNC_WORKERS",
        description="Background worker threads for mailbox sync/import jobs.",
    )
    gmail_message_fetch_workers: int = Field(
        default=8,
        validation_alias="GMAIL_MESSAGE_FETCH_WORKERS",
        description="Per-sync concurrent Gmail message fetches.",
    )

    ai_cache_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "ai_cache.json",
        validation_alias="AI_CACHE_PATH",
    )
    ai_classification_batch_size: int = Field(
        default=20,
        ge=1,
        le=64,
        validation_alias="AI_CLASSIFICATION_BATCH_SIZE",
        description="Max emails per OpenRouter request for inbox AI classification prefetch.",
    )
    ai_cache_ttl_hours: int = Field(default=24, validation_alias="AI_CACHE_TTL_HOURS")
    ai_daily_budget_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "ai_daily_budget.json",
        validation_alias="AI_DAILY_BUDGET_PATH",
    )

    google_userinfo_url: str = Field(
        default="https://www.googleapis.com/oauth2/v3/userinfo",
        validation_alias="GOOGLE_USERINFO_URL",
    )
    auth_error_log_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent.parent / "auth_error.log",
        validation_alias="AUTH_ERROR_LOG_PATH",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, v: str | List[str]) -> str:
        if isinstance(v, list):
            return ",".join(v)
        return v

    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def oauth_scope_list(self) -> List[str]:
        return [s.strip() for s in self.oauth_scopes.split(",") if s.strip()]

    def cors_allow_methods(self) -> List[str]:
        return ["GET", "POST", "PUT", "DELETE", "OPTIONS"]

    def cors_allow_headers(self) -> List[str]:
        return ["Authorization", "Content-Type", "X-Request-ID", "X-MailPulse-Mailbox"]

    @field_validator("session_secret", mode="after")
    @classmethod
    def session_secret_min_length(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if len(raw) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        return v

    @field_validator("oauth_token_fernet_key", mode="after")
    @classmethod
    def fernet_key_well_formed(cls, v: SecretStr) -> SecretStr:
        from cryptography.fernet import Fernet

        key = v.get_secret_value().encode("ascii")
        Fernet(key)  # validates key
        return v


def resolved_env_file_path() -> Path:
    """Absolute path to ``backend/.env`` (the file Pydantic ``Settings`` reads)."""
    return _DEFAULT_ENV_FILE


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton for the worker process."""
    return Settings()
