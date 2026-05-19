"""Custom + system bucket CRUD."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request

from app.container import MailPulseContainer
from app.core.exceptions import ConflictException, DatabaseException, NotFoundException
from app.core.middleware.auth import get_supabase_user_id, require_supabase_user
from app.deps import get_mailpulse_container
from app.schemas.buckets import MailBucketCreate, MailBucketUpdate

router = APIRouter(prefix="/buckets", tags=["buckets"], dependencies=[Depends(require_supabase_user)])


def _norm_name(name: str) -> str:
    return (name or "").strip().lower()


def _name_taken(rows: List[Dict[str, Any]], name: str, exclude_id: str | None = None) -> bool:
    n = _norm_name(name)
    for b in rows:
        if exclude_id and str(b.get("id")) == exclude_id:
            continue
        if _norm_name(str(b.get("name") or "")) == n:
            return True
    return False


def _other_bucket_id(rows: List[Dict[str, Any]]) -> str | None:
    for b in rows:
        if b.get("is_system") and str(b.get("name")) == "Other":
            return str(b["id"])
    return None


def _pg_unique_violation(exc: BaseException | None) -> bool:
    """Detect Postgres unique constraint failures from Supabase/PostgREST error chains."""
    depth = 0
    cur: BaseException | None = exc
    parts: list[str] = []
    while cur is not None and depth < 16:
        parts.append(str(cur))
        msg = getattr(cur, "message", None)
        if isinstance(msg, str):
            parts.append(msg)
        details = getattr(cur, "details", None)
        if isinstance(details, str):
            parts.append(details)
        cur = cur.__cause__ or cur.__context__
        depth += 1
    blob = " ".join(parts).lower()
    return "23505" in blob or "unique constraint" in blob or "duplicate key" in blob


@router.get("")
def list_buckets(request: Request, container: MailPulseContainer = Depends(get_mailpulse_container)):
    uid = get_supabase_user_id(request)
    return {"buckets": container.bucket_repo.get_buckets(uid, request=request)}


@router.post("")
def create_bucket(
    body: MailBucketCreate,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    name = (body.name or "").strip()
    if not name:
        raise ConflictException("Bucket name is required")

    payload = body.model_dump(exclude_none=True)
    payload["name"] = name
    try:
        row = container.bucket_repo.create_custom_bucket(uid, payload, request=request)
    except DatabaseException as de:
        if _pg_unique_violation(de) or _pg_unique_violation(de.__cause__) or _pg_unique_violation(de.__context__):
            raise ConflictException("A bucket with this name already exists") from de
        raise

    return {"bucket": row}


@router.patch("/{bucket_id}")
def update_bucket(
    bucket_id: str,
    body: MailBucketUpdate,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    row = container.bucket_repo.get_bucket_by_id(uid, bucket_id, request=request)
    if not row or row.get("is_system"):
        raise NotFoundException("Bucket not found or cannot be edited")

    data = body.model_dump(exclude_unset=True)
    if not data:
        return {"bucket": row}

    if "name" in data and data["name"]:
        existing = container.bucket_repo.get_buckets(uid, request=request)
        if _name_taken(existing, data["name"], exclude_id=bucket_id):
            raise ConflictException("A bucket with this name already exists")

    updated = container.bucket_repo.update_custom_bucket(uid, bucket_id, data, request=request)
    return {"bucket": updated}


@router.delete("/{bucket_id}")
def delete_bucket(
    bucket_id: str,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    row = container.bucket_repo.get_bucket_by_id(uid, bucket_id, request=request)
    if not row or row.get("is_system"):
        raise NotFoundException("Bucket not found or cannot be deleted")

    all_buckets = container.bucket_repo.get_buckets(uid, request=request)
    other_id = _other_bucket_id(all_buckets)
    if not other_id:
        raise ConflictException("System bucket 'Other' not available; cannot delete safely")

    container.bucket_repo.reassign_email_bucket_assignments(uid, bucket_id, other_id, request=request)
    container.bucket_repo.delete_custom_bucket(uid, bucket_id, request=request)
    return {"success": True, "reassigned_to": "Other"}
