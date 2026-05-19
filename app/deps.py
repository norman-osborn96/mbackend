"""FastAPI dependency providers."""

from __future__ import annotations

from fastapi import Request

from app.container import MailPulseContainer, get_container


def get_mailpulse_container(request: Request) -> MailPulseContainer:
    """Prefer app.state (lifespan); fall back to process singleton (tests / edge cases)."""
    c = getattr(request.app.state, "container", None)
    if c is not None:
        return c  # type: ignore[no-any-return]
    return get_container()
