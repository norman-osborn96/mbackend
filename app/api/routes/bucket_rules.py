"""Per-user bucket automation rules."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.container import MailPulseContainer
from app.core.exceptions import NotFoundException
from app.core.middleware.auth import get_supabase_user_id, require_supabase_user
from app.deps import get_mailpulse_container
from app.schemas.buckets import MailBucketRuleCreate, MailBucketRuleUpdate

router = APIRouter(prefix="/bucket-rules", tags=["bucket-rules"], dependencies=[Depends(require_supabase_user)])


def _ensure_bucket_assignable(container: MailPulseContainer, uid: str, bucket_id: UUID, request: Request) -> None:
    if not container.bucket_repo.get_bucket_by_id(uid, str(bucket_id), request=request):
        raise NotFoundException("Bucket not found")


@router.get("")
def list_rules(request: Request, container: MailPulseContainer = Depends(get_mailpulse_container)):
    uid = get_supabase_user_id(request)
    return {"rules": container.bucket_repo.get_bucket_rules(uid, request=request)}


@router.post("")
def create_rule(
    body: MailBucketRuleCreate,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    _ensure_bucket_assignable(container, uid, body.bucket_id, request)
    row = container.bucket_repo.create_bucket_rule(uid, body.model_dump(), request=request)
    return {"rule": row}


@router.patch("/{rule_id}")
def update_rule(
    rule_id: str,
    body: MailBucketRuleUpdate,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    data = body.model_dump(exclude_unset=True)
    if data.get("bucket_id") is not None:
        _ensure_bucket_assignable(container, uid, UUID(str(data["bucket_id"])), request)
    row = container.bucket_repo.update_bucket_rule(uid, rule_id, data, request=request)
    return {"rule": row}


@router.delete("/{rule_id}")
def delete_rule(
    rule_id: str,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    container.bucket_repo.delete_bucket_rule(uid, rule_id, request=request)
    return {"success": True}
