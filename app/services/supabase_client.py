"""Create a Supabase client (used by tests or scripts; app uses MailPulseContainer)."""

from __future__ import annotations

from supabase import Client, create_client

from app.core.config import Settings


def create_supabase_client(settings: Settings) -> Client:
    return create_client(settings.supabase_url, settings.supabase_key.get_secret_value())
