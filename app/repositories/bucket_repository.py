import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

from starlette.requests import Request

from app.core.exceptions import DatabaseException
from app.core.logging import get_logger
from app.core.supabase_request_context import MAILPULSE_USER_SUPABASE_CLIENT_STATE_ATTR
from app.repositories.supabase_repository import SupabaseRepository

log = get_logger("bucket_repository", service_name="BucketRepository")

_T = TypeVar("_T")


def _is_transient_network_exc(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "10054" in msg or "forcibly closed" in msg:
        return True
    if any(x in msg for x in ("connection reset", "broken pipe", "connection aborted", "eof occurred")):
        return True
    if "timed out" in msg or "timeout" in msg:
        return True
    name = type(exc).__name__.lower()
    if "remoteprotocol" in name or "protocolerror" in name:
        return True
    return False


def _call_with_transient_retry(fn: Callable[[], _T], *, attempts: int = 3, base_delay: float = 0.15) -> _T:
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt >= attempts - 1 or not _is_transient_network_exc(e):
                raise
            time.sleep(base_delay * (2**attempt))
    assert last is not None
    raise last


class BucketRepository(SupabaseRepository):
    """Pass ``request`` from FastAPI routes so PostgREST uses anon + JWT (RLS). Background jobs omit it → service_role."""

    def _effective_sb(self, request: Request | None) -> Any:
        if request is not None:
            sb = getattr(request.state, MAILPULSE_USER_SUPABASE_CLIENT_STATE_ATTR, None)
            if sb is not None:
                return sb
        return self._client

    def get_buckets(self, user_id: str, *, request: Request | None = None) -> List[Dict[str, Any]]:
        """Fetch all system buckets and custom buckets for the user.

        Uses two queries instead of a single ``or_()`` filter — PostgREST ``or`` filters
        with mixed boolean + UUID columns are easy to mis-parse and return errors.
        """
        sb = self._effective_sb(request)

        def _fetch_merged() -> List[Dict[str, Any]]:
            system_resp = (
                sb.table("mail_buckets")
                .select("*")
                .eq("is_system", True)
                .order("sort_order", desc=False)
                .order("created_at", desc=False)
                .execute()
            )
            custom_resp = (
                sb.table("mail_buckets")
                .select("*")
                .eq("is_system", False)
                .eq("user_id", user_id)
                .order("sort_order", desc=False)
                .order("created_at", desc=False)
                .execute()
            )
            system_rows = list(system_resp.data or [])
            custom_rows = list(custom_resp.data or [])
            merged_local = system_rows + custom_rows
            merged_local.sort(
                key=lambda r: (
                    int(r.get("sort_order") or 0),
                    str(r.get("created_at") or ""),
                )
            )
            return merged_local

        try:
            return _call_with_transient_retry(_fetch_merged)
        except Exception as e:
            log.error("Failed to fetch buckets: %s", e)
            raise DatabaseException("Failed to fetch buckets") from e

    def create_custom_bucket(
        self, user_id: str, data: Dict[str, Any], *, request: Request | None = None
    ) -> Dict[str, Any]:
        """Insert a user-owned bucket row. Authenticated API routes use anon key + JWT so RLS passes."""
        uid = str(user_id or "").strip()
        if not uid:
            raise DatabaseException("Missing authenticated user id for bucket creation")

        name = str(data.get("name") or "").strip()
        if not name:
            raise DatabaseException("Bucket name is required")

        row_in: Dict[str, Any] = {
            "user_id": uid,
            "name": name,
            "is_system": False,
            # Keep custom buckets after seeded system sort_order values (max seed is 14).
            "sort_order": int(data["sort_order"]) if data.get("sort_order") is not None else 100,
        }
        for key in ("description", "color", "icon"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                row_in[key] = val.strip()

        sb = self._effective_sb(request)
        try:
            # postgrest-py: insert() returns QueryRequestBuilder (no .select chain).
            # Prefer already includes return=representation so execute() yields the new row.
            response = sb.table("mail_buckets").insert(row_in).execute()
            if not response.data:
                raise DatabaseException("Bucket creation returned no data")
            return response.data[0]
        except DatabaseException:
            raise
        except Exception as e:
            log.exception("Failed to create custom bucket for user_id=%s name=%s", uid, name)
            raise DatabaseException(
                "Failed to create custom bucket. Apply backend/sql/supabase_mail_buckets_setup.sql in "
                "the Supabase SQL editor. Set SUPABASE_ANON_KEY to the **anon** API key and SUPABASE_KEY "
                "to **service_role** (Project Settings → API), then restart the API."
            ) from e

    def update_custom_bucket(
        self, user_id: str, bucket_id: str, data: Dict[str, Any], *, request: Request | None = None
    ) -> Dict[str, Any]:
        sb = self._effective_sb(request)
        try:
            response = (
                sb.table("mail_buckets")
                .update(data)
                .eq("id", bucket_id)
                .eq("user_id", user_id)
                .eq("is_system", False)
                .execute()
            )
            if not response.data:
                raise DatabaseException("Bucket update failed or bucket not found")
            return response.data[0]
        except Exception as e:
            log.error("Failed to update custom bucket: %s", e)
            raise DatabaseException("Failed to update custom bucket") from e

    def delete_custom_bucket(self, user_id: str, bucket_id: str, *, request: Request | None = None) -> None:
        sb = self._effective_sb(request)
        try:
            sb.table("mail_buckets").delete().eq("id", bucket_id).eq("user_id", user_id).eq("is_system", False).execute()
        except Exception as e:
            log.error("Failed to delete custom bucket: %s", e)
            raise DatabaseException("Failed to delete custom bucket") from e

    def get_bucket_by_id(
        self, user_id: str, bucket_id: str, *, request: Request | None = None
    ) -> Optional[Dict[str, Any]]:
        """Return bucket row if it is system-wide or owned by this user."""
        sb = self._effective_sb(request)
        try:
            response = sb.table("mail_buckets").select("*").eq("id", bucket_id).limit(1).execute()
            row = response.data[0] if response.data else None
            if not row:
                return None
            if row.get("is_system"):
                return row
            if str(row.get("user_id") or "") == str(user_id):
                return row
            return None
        except Exception as e:
            log.error("Failed to fetch bucket: %s", e)
            raise DatabaseException("Failed to fetch bucket") from e

    def reassign_email_bucket_assignments(
        self, user_id: str, from_bucket_id: str, to_bucket_id: str, *, request: Request | None = None
    ) -> None:
        sb = self._effective_sb(request)
        try:
            (
                sb.table("email_bucket_assignments")
                .update({"bucket_id": to_bucket_id})
                .eq("user_id", user_id)
                .eq("bucket_id", from_bucket_id)
                .execute()
            )
        except Exception as e:
            log.error("Failed to reassign bucket assignments: %s", e)
            raise DatabaseException("Failed to reassign bucket assignments") from e

    def get_assignment_for_message(
        self, user_id: str, gmail_message_id: str, *, request: Request | None = None
    ) -> Optional[Dict[str, Any]]:
        sb = self._effective_sb(request)
        try:
            response = (
                sb.table("email_bucket_assignments")
                .select("*, mail_buckets(name)")
                .eq("user_id", user_id)
                .eq("gmail_message_id", gmail_message_id)
                .limit(1)
                .execute()
            )
            return response.data[0] if response.data else None
        except Exception as e:
            log.error("Failed to fetch bucket assignment: %s", e)
            raise DatabaseException("Failed to fetch bucket assignment") from e

    def get_email_bucket_assignments(
        self, user_id: str, gmail_message_ids: List[str], *, request: Request | None = None
    ) -> List[Dict[str, Any]]:
        if not gmail_message_ids:
            return []
        sb = self._effective_sb(request)
        try:
            response = (
                sb.table("email_bucket_assignments")
                .select("*, mail_buckets(name, color, icon)")
                .eq("user_id", user_id)
                .in_("gmail_message_id", gmail_message_ids)
                .execute()
            )
            return response.data
        except Exception as e:
            log.error("Failed to fetch bucket assignments: %s", e)
            raise DatabaseException("Failed to fetch bucket assignments") from e

    def upsert_email_bucket_assignment(
        self, user_id: str, data: Dict[str, Any], *, request: Request | None = None
    ) -> Dict[str, Any]:
        sb = self._effective_sb(request)
        try:
            data["user_id"] = user_id
            response = (
                sb.table("email_bucket_assignments")
                .upsert(data, on_conflict="user_id,gmail_message_id")
                .execute()
            )
            if not response.data:
                raise DatabaseException("Bucket assignment upsert returned no data")
            return response.data[0]
        except Exception as e:
            log.error("Failed to upsert bucket assignment: %s", e)
            raise DatabaseException("Failed to upsert bucket assignment") from e

    def get_bucket_rules(self, user_id: str, *, request: Request | None = None) -> List[Dict[str, Any]]:
        sb = self._effective_sb(request)
        try:
            response = (
                sb.table("mail_bucket_rules")
                .select("*, mail_buckets(name)")
                .eq("user_id", user_id)
                .order("priority", desc=True)
                .execute()
            )
            return response.data
        except Exception as e:
            log.error("Failed to fetch bucket rules: %s", e)
            raise DatabaseException("Failed to fetch bucket rules") from e

    def create_bucket_rule(self, user_id: str, data: Dict[str, Any], *, request: Request | None = None) -> Dict[str, Any]:
        sb = self._effective_sb(request)
        try:
            data["user_id"] = user_id
            response = sb.table("mail_bucket_rules").insert(data).execute()
            if not response.data:
                raise DatabaseException("Bucket rule creation returned no data")
            return response.data[0]
        except Exception as e:
            log.error("Failed to create bucket rule: %s", e)
            raise DatabaseException("Failed to create bucket rule") from e

    def update_bucket_rule(
        self, user_id: str, rule_id: str, data: Dict[str, Any], *, request: Request | None = None
    ) -> Dict[str, Any]:
        sb = self._effective_sb(request)
        try:
            response = (
                sb.table("mail_bucket_rules")
                .update(data)
                .eq("id", rule_id)
                .eq("user_id", user_id)
                .execute()
            )
            if not response.data:
                raise DatabaseException("Bucket rule update failed or rule not found")
            return response.data[0]
        except Exception as e:
            log.error("Failed to update bucket rule: %s", e)
            raise DatabaseException("Failed to update bucket rule") from e

    def delete_bucket_rule(self, user_id: str, rule_id: str, *, request: Request | None = None) -> None:
        sb = self._effective_sb(request)
        try:
            sb.table("mail_bucket_rules").delete().eq("id", rule_id).eq("user_id", user_id).execute()
        except Exception as e:
            log.error("Failed to delete bucket rule: %s", e)
            raise DatabaseException("Failed to delete bucket rule") from e
