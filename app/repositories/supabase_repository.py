"""Base repository for Supabase-backed persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.logging import get_logger

if TYPE_CHECKING:
    from supabase import Client

log = get_logger("supabase_repository", service_name="SupabaseRepository")


class SupabaseRepository:
    """Shared Supabase client access with async context manager lifecycle."""

    def __init__(self, client: "Client") -> None:
        self._client = client

    @property
    def client(self) -> "Client":
        return self._client

    async def __aenter__(self) -> "SupabaseRepository":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client when the SDK exposes a close hook."""
        close_fn = getattr(self._client, "close", None)
        if not callable(close_fn):
            return
        try:
            result = close_fn()
            if hasattr(result, "__await__"):
                await result  # type: ignore[func-returns-value]
        except Exception as e:
            log.warning("Supabase client close failed: %s", e)
