"""Data access layer (Supabase and other backends)."""

from app.repositories.sender_repository import SenderRepository
from app.repositories.supabase_repository import SupabaseRepository

__all__ = ["SupabaseRepository", "SenderRepository"]
