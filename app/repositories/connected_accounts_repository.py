"""Supabase persistence for linked Gmail accounts (encrypted OAuth tokens per user)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.core.logging import get_logger
from app.repositories.supabase_repository import SupabaseRepository
from app.services import oauth_token_crypto

if TYPE_CHECKING:
    from supabase import Client

log = get_logger("connected_accounts_repository", service_name="ConnectedAccountsRepository")


class ConnectedAccountsRepository(SupabaseRepository):
    def __init__(self, client: "Client") -> None:
        super().__init__(client)

    def list_accounts(self, user_id: str) -> List[Dict[str, Any]]:
        uid = (user_id or "").strip()
        if not uid:
            return []
        try:
            res = (
                self.client.table("connected_gmail_accounts")
                .select("account_email,updated_at")
                .eq("user_id", uid)
                .order("updated_at", desc=True)
                .execute()
            )
            return list(res.data or [])
        except Exception as e:
            log.warning("connected_gmail_accounts list failed: %s", e)
            return []

    def get_decrypted_credentials(self, user_id: str, account_email: str) -> Optional[Dict[str, Any]]:
        uid = (user_id or "").strip()
        key = (account_email or "").strip().lower()
        if not uid or not key:
            return None
        try:
            res = (
                self.client.table("connected_gmail_accounts")
                .select("credentials_encrypted")
                .eq("user_id", uid)
                .eq("account_email", key)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if not rows:
                return None
            blob = rows[0].get("credentials_encrypted")
            if not blob or not isinstance(blob, str):
                return None
            return oauth_token_crypto.decrypt_credentials_blob(blob)
        except Exception as e:
            log.warning("connected_gmail_accounts read failed: %s", e)
            return None

    def upsert_account(self, user_id: str, account_email: str, creds_dict: Dict[str, Any]) -> None:
        uid = (user_id or "").strip()
        key = (account_email or "").strip().lower()
        if not uid or not key:
            return
        blob = oauth_token_crypto.encrypt_credentials_blob(creds_dict)
        self.client.table("connected_gmail_accounts").upsert(
            {"user_id": uid, "account_email": key, "credentials_encrypted": blob},
            on_conflict="user_id,account_email",
        ).execute()

    def delete_account(self, user_id: str, account_email: str) -> None:
        uid = (user_id or "").strip()
        key = (account_email or "").strip().lower()
        if not uid or not key:
            return
        self.client.table("connected_gmail_accounts").delete().eq("user_id", uid).eq("account_email", key).execute()
