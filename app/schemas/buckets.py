from typing import Optional
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class MailBucketBase(BaseModel):
    name: str = Field(..., max_length=100)
    description: Optional[str] = Field(None, max_length=255)
    color: Optional[str] = Field(None, max_length=50)
    icon: Optional[str] = Field(None, max_length=50)


class MailBucketCreate(MailBucketBase):
    pass


class MailBucketUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=255)
    color: Optional[str] = Field(None, max_length=50)
    icon: Optional[str] = Field(None, max_length=50)


class MailBucketResponse(MailBucketBase):
    id: UUID
    user_id: Optional[UUID] = None
    is_system: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EmailBucketAssignmentBase(BaseModel):
    bucket_id: UUID
    bucket_source: str = Field(default="MANUAL")
    bucket_locked: bool = Field(default=True)
    reason: Optional[str] = None


class EmailBucketAssignmentCreate(EmailBucketAssignmentBase):
    gmail_message_id: str


class EmailBucketAssignmentResponse(EmailBucketAssignmentBase):
    id: UUID
    user_id: UUID
    gmail_message_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MailBucketRuleBase(BaseModel):
    bucket_id: UUID
    field_name: str
    operator: str
    value: str
    priority: int = 0
    is_active: bool = True


class MailBucketRuleCreate(MailBucketRuleBase):
    pass


class MailBucketRuleUpdate(BaseModel):
    bucket_id: Optional[UUID] = None
    field_name: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None


class MailBucketRuleResponse(MailBucketRuleBase):
    id: UUID
    user_id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


_BUCKET_SOURCES_FROZEN = frozenset({"MANUAL", "RULE", "AI", "SYSTEM"})


class EmailBucketMoveBody(BaseModel):
    bucket_id: UUID
    bucket_source: str = Field(default="MANUAL")
    bucket_locked: bool = Field(default=True)
    reason: Optional[str] = Field(None, max_length=500)

    @field_validator("bucket_source")
    @classmethod
    def normalize_source(cls, v: str) -> str:
        up = (v or "MANUAL").strip().upper()
        if up not in _BUCKET_SOURCES_FROZEN:
            raise ValueError(f"bucket_source must be one of: {', '.join(sorted(_BUCKET_SOURCES_FROZEN))}")
        return up
