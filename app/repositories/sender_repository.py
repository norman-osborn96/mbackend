"""VIP / medium-priority sender rows in Supabase."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.core.exceptions import DatabaseException
from app.core.logging import get_logger
from app.repositories.supabase_repository import SupabaseRepository

if TYPE_CHECKING:
    from supabase import Client

log = get_logger("sender_repository", service_name="SenderRepository")


class SenderRepository(SupabaseRepository):
    """All reads and writes for sender priority tables go through this repository."""

    def list_vip_emails(self) -> List[str]:
        try:
            res = self.client.table("vip_senders").select("email").execute()
            return [str(row["email"]).strip().lower() for row in (res.data or [])]
        except Exception as e:
            log.exception("Supabase vip_senders select failed")
            raise DatabaseException("Failed to load VIP senders", details={"table": "vip_senders"}) from e

    def list_medium_emails(self) -> List[str]:
        try:
            res = self.client.table("medium_priority_senders").select("email").execute()
            return [str(row["email"]).strip().lower() for row in (res.data or [])]
        except Exception as e:
            log.exception("Supabase medium_priority_senders select failed")
            raise DatabaseException(
                "Failed to load medium-priority senders",
                details={"table": "medium_priority_senders"},
            ) from e

    def upsert_vip_exclusive(self, normalized_email: str) -> None:
        try:
            self.client.table("medium_priority_senders").delete().eq("email", normalized_email).execute()
            self.client.table("vip_senders").upsert({"email": normalized_email}).execute()
        except Exception as e:
            log.exception("Supabase VIP upsert failed")
            raise DatabaseException("Failed to save VIP sender", details={"email": normalized_email}) from e

    def upsert_medium_exclusive(self, normalized_email: str) -> None:
        try:
            self.client.table("vip_senders").delete().eq("email", normalized_email).execute()
            self.client.table("medium_priority_senders").upsert({"email": normalized_email}).execute()
        except Exception as e:
            log.exception("Supabase medium upsert failed")
            raise DatabaseException(
                "Failed to save medium-priority sender",
                details={"email": normalized_email},
            ) from e

    def delete_vip(self, normalized_email: str) -> None:
        try:
            self.client.table("vip_senders").delete().eq("email", normalized_email).execute()
        except Exception as e:
            log.exception("Supabase VIP delete failed")
            raise DatabaseException("Failed to remove VIP sender", details={"email": normalized_email}) from e

    def delete_medium(self, normalized_email: str) -> None:
        try:
            self.client.table("medium_priority_senders").delete().eq("email", normalized_email).execute()
        except Exception as e:
            log.exception("Supabase medium delete failed")
            raise DatabaseException(
                "Failed to remove medium-priority sender",
                details={"email": normalized_email},
            ) from e

    def get_user_ai_profile(self, account_email: str) -> Optional[Dict[str, Any]]:
        key = (account_email or "").strip().lower()
        if not key:
            return None
        try:
            res = self.client.table("user_ai_profiles").select("profile").eq("account_email", key).limit(1).execute()
            rows = res.data or []
            if not rows:
                return None
            prof = rows[0].get("profile")
            return prof if isinstance(prof, dict) else None
        except Exception as e:
            log.warning("user_ai_profiles select failed (table may not exist yet): %s", e)
            return None

    def upsert_user_ai_profile(self, account_email: str, profile: Dict[str, Any]) -> None:
        key = (account_email or "").strip().lower()
        if not key:
            return
        try:
            self.client.table("user_ai_profiles").upsert(
                {"account_email": key, "profile": profile},
                on_conflict="account_email",
            ).execute()
        except Exception as e:
            log.exception("user_ai_profiles upsert failed")
            raise DatabaseException(
                "Failed to save AI profile",
                details={"account_email": key},
            ) from e
