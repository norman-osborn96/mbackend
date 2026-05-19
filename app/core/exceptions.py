"""Typed application exceptions for consistent HTTP error mapping."""

from __future__ import annotations

from typing import Any, Optional


class MailPulseBaseException(Exception):
    """Base class for all MailPulse domain errors."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}


class EmailSyncException(MailPulseBaseException):
    code = "EMAIL_SYNC_FAILED"
    status_code = 502


class AIClassificationException(MailPulseBaseException):
    code = "AI_CLASSIFICATION_FAILED"
    status_code = 502


class AuthException(MailPulseBaseException):
    code = "AUTH_FAILED"
    status_code = 401


class RateLimitException(MailPulseBaseException):
    code = "RATE_LIMITED"
    status_code = 429


class ValidationException(MailPulseBaseException):
    code = "VALIDATION_ERROR"
    status_code = 400


class NotFoundException(MailPulseBaseException):
    code = "NOT_FOUND"
    status_code = 404


class ConflictException(MailPulseBaseException):
    code = "CONFLICT"
    status_code = 409


class DatabaseException(MailPulseBaseException):
    code = "DATABASE_ERROR"
    status_code = 503
