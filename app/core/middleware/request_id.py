"""HTTP middleware (request ID propagation)."""

from __future__ import annotations

import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import set_request_context


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign X-Request-ID and attach to logging context."""

    header_name = "X-Request-ID"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        incoming = request.headers.get(self.header_name)
        rid = incoming or str(uuid.uuid4())
        user_id = "-"
        try:
            sess = request.scope.get("session")
            if isinstance(sess, dict):
                prof = sess.get("user_profile")
                if isinstance(prof, dict) and prof.get("email"):
                    user_id = str(prof["email"])
        except Exception:
            user_id = "-"
        set_request_context(request_id=rid, user_id=user_id)
        response = await call_next(request)
        response.headers[self.header_name] = rid
        return response
