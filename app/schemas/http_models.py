"""Shared Pydantic request bodies with validation and sanitization."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.security import sanitize_text

_EMAIL_MAX = 254
_TEXT_SUBJECT = 998
_TEXT_SNIPPET = 16_384
_TEXT_BODY = 100_000
_THREAD_ID = 128


class SuggestReplyBody(BaseModel):
    subject: str = Field(default="", max_length=_TEXT_SUBJECT)
    snippet: str = Field(default="", max_length=_TEXT_SNIPPET)
    sender: str = Field(default="", max_length=_EMAIL_MAX)
    regenerate: bool = Field(
        default=False,
        description="Bypass server reply cache and call the model again (Regenerate in UI).",
    )

    @field_validator("subject", mode="before")
    @classmethod
    def _subject(cls, v: object) -> str:
        return sanitize_text(str(v or ""), max_length=_TEXT_SUBJECT)

    @field_validator("snippet", mode="before")
    @classmethod
    def _snippet(cls, v: object) -> str:
        return sanitize_text(str(v or ""), max_length=_TEXT_SNIPPET)

    @field_validator("sender", mode="before")
    @classmethod
    def _sender(cls, v: object) -> str:
        return sanitize_text(str(v or ""), max_length=_EMAIL_MAX)


class SendReplyBody(BaseModel):
    to: EmailStr = Field(max_length=_EMAIL_MAX)
    subject: str = Field(max_length=_TEXT_SUBJECT)
    body: str = Field(max_length=_TEXT_BODY)
    thread_id: str = Field(default="", max_length=_THREAD_ID)
    message_id_header: str = Field(default="", max_length=_THREAD_ID)

    @field_validator("subject", mode="before")
    @classmethod
    def _subject(cls, v: object) -> str:
        return sanitize_text(str(v or ""), max_length=_TEXT_SUBJECT)

    @field_validator("body", mode="before")
    @classmethod
    def _body(cls, v: object) -> str:
        return sanitize_text(str(v or ""), max_length=_TEXT_BODY)

    @field_validator("thread_id", "message_id_header", mode="before")
    @classmethod
    def _ids(cls, v: object) -> str:
        return sanitize_text(str(v or ""), max_length=_THREAD_ID)


class SenderEmailRuleBody(BaseModel):
    sender_email: EmailStr = Field(max_length=_EMAIL_MAX)


class PrioritySenderBody(BaseModel):
    email: EmailStr = Field(max_length=_EMAIL_MAX)
