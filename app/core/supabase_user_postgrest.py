"""User-scoped PostgREST access (anon apikey + access JWT) without Supabase Client / GoTrue side effects."""

from __future__ import annotations

from postgrest import SyncPostgrestClient


class MailPulsePostgrestBridge:
    """Minimal ``Client.table``-compatible wrapper around ``SyncPostgrestClient``."""

    __slots__ = ("_pc",)

    def __init__(self, pc: SyncPostgrestClient) -> None:
        self._pc = pc

    def table(self, name: str):
        return self._pc.from_(name)


def build_user_postgrest_bridge(
    supabase_base_url: str,
    anon_key: str,
    access_token: str,
    *,
    schema: str = "public",
) -> MailPulsePostgrestBridge:
    """PostgREST client that sends the user's JWT so ``auth.uid()`` matches RLS policies."""
    rest_url = f"{supabase_base_url.rstrip('/')}/rest/v1"
    headers = {
        "apikey": anon_key.strip(),
        "Authorization": f"Bearer {access_token.strip()}",
    }
    return MailPulsePostgrestBridge(SyncPostgrestClient(rest_url, headers=headers, schema=schema))
