"""Supabase-backed cache of Gmail message envelopes (per user + mailbox)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

from app.core.logging import get_logger
from app.repositories.supabase_repository import SupabaseRepository

if TYPE_CHECKING:
    from supabase import Client

log = get_logger("gmail_message_index_repository", service_name="GmailMessageIndexRepository")

_CHUNK = 80


class GmailMessageIndexRepository(SupabaseRepository):
    def __init__(self, client: "Client") -> None:
        super().__init__(client)

    def bulk_upsert_envelopes(
        self, user_id: str, account_email: str, envelopes: List[Dict[str, Any]]
    ) -> None:
        uid = (user_id or "").strip()
        key = (account_email or "").strip().lower()
        if not uid or not key or not envelopes:
            return
        rows: List[Dict[str, Any]] = []
        for m in envelopes:
            mid = m.get("id")
            if not mid:
                continue
            try:
                ts = int(m.get("internal_date") or 0)
            except (TypeError, ValueError):
                ts = 0
            rows.append(
                {
                    "user_id": uid,
                    "account_email": key,
                    "message_id": str(mid),
                    "internal_ts": ts,
                    "envelope": m,
                }
            )
        if not rows:
            return
        try:
            for i in range(0, len(rows), _CHUNK):
                chunk = rows[i : i + _CHUNK]
                self.client.table("gmail_message_index").upsert(
                    chunk,
                    on_conflict="user_id,account_email,message_id",
                ).execute()
            log.info("gmail_message_index upsert | %s | %s | n=%d", uid, key, len(rows))
        except Exception as e:
            log.warning("gmail_message_index upsert failed: %s", e)

    def list_envelopes(self, user_id: str, account_email: str, limit: int = 500) -> List[Dict[str, Any]]:
        uid = (user_id or "").strip()
        key = (account_email or "").strip().lower()
        if not uid or not key:
            return []
        lim = max(1, min(2000, int(limit)))
        try:
            res = (
                self.client.table("gmail_message_index")
                .select("envelope")
                .eq("user_id", uid)
                .eq("account_email", key)
                .order("internal_ts", desc=True)
                .limit(lim)
                .execute()
            )
            out: List[Dict[str, Any]] = []
            for row in res.data or []:
                env = row.get("envelope")
                if isinstance(env, dict):
                    out.append(env)
            return out
        except Exception as e:
            log.warning("gmail_message_index select failed: %s", e)
            return []

    def list_envelopes_page(
        self,
        user_id: str,
        account_email: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        uid = (user_id or "").strip()
        key = (account_email or "").strip().lower()
        lim = max(1, min(500, int(limit)))
        start = max(0, int(offset))
        if not uid or not key:
            return {"emails": [], "next_offset": None, "has_more": False}
        try:
            res = (
                self.client.table("gmail_message_index")
                .select("envelope")
                .eq("user_id", uid)
                .eq("account_email", key)
                .order("internal_ts", desc=True)
                .range(start, start + lim)
                .execute()
            )
            rows = list(res.data or [])
            has_more = len(rows) > lim
            out: List[Dict[str, Any]] = []
            for row in rows[:lim]:
                env = row.get("envelope")
                if isinstance(env, dict):
                    out.append(env)
            return {
                "emails": out,
                "next_offset": start + lim if has_more else None,
                "has_more": has_more,
            }
        except Exception as e:
            log.warning("gmail_message_index page select failed: %s", e)
            return {"emails": [], "next_offset": None, "has_more": False}

    def delete_mailbox(self, user_id: str, account_email: str) -> None:
        uid = (user_id or "").strip()
        key = (account_email or "").strip().lower()
        if not uid or not key:
            return
        try:
            self.client.table("gmail_message_index").delete().eq("user_id", uid).eq("account_email", key).execute()
        except Exception as e:
            log.warning("gmail_message_index delete failed: %s", e)

    def delete_messages(self, user_id: str, account_email: str, message_ids: List[str]) -> None:
        uid = (user_id or "").strip()
        key = (account_email or "").strip().lower()
        ids = [str(mid) for mid in message_ids if mid]
        if not uid or not key or not ids:
            return
        try:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i : i + _CHUNK]
                (
                    self.client.table("gmail_message_index")
                    .delete()
                    .eq("user_id", uid)
                    .eq("account_email", key)
                    .in_("message_id", chunk)
                    .execute()
                )
        except Exception as e:
            log.warning("gmail_message_index delete messages failed: %s", e)
