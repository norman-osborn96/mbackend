"""Middleware package (avoids shadowing ``middleware.py``)."""

from app.core.middleware.request_id import RequestIdMiddleware

__all__ = ["RequestIdMiddleware"]
