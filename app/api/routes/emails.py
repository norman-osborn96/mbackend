from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from app.container import MailPulseContainer
from app.core.exceptions import NotFoundException
from app.core.middleware.auth import get_supabase_user_id, require_supabase_user
from app.core.rate_limit import limiter
from app.deps import get_mailpulse_container
from app.schemas.buckets import EmailBucketMoveBody
from app.schemas.http_models import SendReplyBody, SuggestReplyBody
from app.services import session_credentials

router = APIRouter(prefix="/emails", dependencies=[Depends(require_supabase_user)])


@router.get("")
def get_emails(
    request: Request,
    background_tasks: BackgroundTasks,
    container: MailPulseContainer = Depends(get_mailpulse_container),
    refresh: bool = False,
    since: Optional[int] = None,
    page: int = 1,
    limit: int = 50,
    pageToken: Optional[str] = None,
):
    creds_dict = session_credentials.load_and_persist_fresh_for_request(request)

    uid = get_supabase_user_id(request)

    def schedule_lazy_watch(creds: dict) -> None:
        background_tasks.add_task(container.email_service.auto_start_gmail_watch, creds, uid)

    return container.email_service.list_emails(
        creds_dict,
        refresh=refresh,
        since=since,
        page=page,
        limit=limit,
        page_token=pageToken,
        schedule_lazy_watch=schedule_lazy_watch,
        supabase_user_id=uid,
    )


@router.post("/suggest-reply")
@limiter.limit("120/minute")
def suggest_reply(
    body: SuggestReplyBody,
    request: Request,
    response: Response,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    creds_dict = session_credentials.load_and_persist_fresh_for_request(request)
    suggestion = container.email_service.suggest_reply(
        creds_dict,
        subject=body.subject,
        snippet=body.snippet,
        sender=body.sender,
        regenerate=body.regenerate,
    )
    return {"suggestion": suggestion}


@router.post("/send-reply")
def send_email_reply(
    body: SendReplyBody,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    creds_dict = session_credentials.load_and_persist_fresh_for_request(request)
    return container.email_service.send_email_reply(
        creds_dict,
        to=str(body.to),
        subject=body.subject,
        body=body.body,
        thread_id=body.thread_id,
        message_id_header=body.message_id_header,
    )


@router.post("/{email_id:path}/bucket")
def move_email_to_bucket(
    email_id: str,
    body: EmailBucketMoveBody,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    row = container.bucket_repo.get_bucket_by_id(uid, str(body.bucket_id), request=request)
    if not row:
        raise NotFoundException("Bucket not found")

    reason = body.reason or (
        "User manually moved this email" if body.bucket_source == "MANUAL" else "Bucket assignment update"
    )

    container.bucket_repo.upsert_email_bucket_assignment(
        uid,
        {
            "gmail_message_id": email_id,
            "bucket_id": str(body.bucket_id),
            "bucket_source": body.bucket_source,
            "bucket_locked": body.bucket_locked,
            "reason": reason,
        },
        request=request,
    )

    return {
        "success": True,
        "gmail_message_id": email_id,
        "bucket": {"id": str(row["id"]), "name": row["name"]},
        "bucket_source": body.bucket_source,
        "bucket_locked": body.bucket_locked,
        "primary_bucket": row["name"],
        "bucket_tags": [row["name"]],
        "bucket_reason": reason,
    }


@router.get("/{email_id:path}/bucket")
def get_email_bucket_meta(
    email_id: str,
    request: Request,
    container: MailPulseContainer = Depends(get_mailpulse_container),
):
    uid = get_supabase_user_id(request)
    a = container.bucket_repo.get_assignment_for_message(uid, email_id, request=request)
    if not a:
        return {"success": True, "gmail_message_id": email_id, "assignment": None}
    nested = a.get("mail_buckets") or {}
    name = nested.get("name") if isinstance(nested, dict) else None
    return {
        "success": True,
        "gmail_message_id": email_id,
        "assignment": {
            "bucket": {"id": str(a["bucket_id"]), "name": name},
            "bucket_source": a.get("bucket_source"),
            "bucket_locked": a.get("bucket_locked"),
            "reason": a.get("reason"),
        },
    }
