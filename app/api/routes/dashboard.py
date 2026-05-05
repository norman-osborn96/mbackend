from fastapi import APIRouter, Request, HTTPException
from app.services.gmail_service import fetch_emails, get_user_email
from app.services.priority_engine import calculate_priority
from app.services import email_cache, watch_state
from app.core.logger import get_logger
from datetime import date

router = APIRouter(prefix="/dashboard")
log = get_logger("dashboard_route")


def _get_all_emails(creds_dict):
    """Return the best available email list — push-cache if available, legacy otherwise."""
    try:
        email_address = get_user_email(creds_dict)
    except Exception:
        email_address = None

    if email_address and watch_state.get(email_address):
        msgs = email_cache.list_all_messages(email_address)
        if msgs:
            return msgs

    # Fallback to legacy fetch
    fetch_result = fetch_emails(creds_dict, force_refresh=False)
    return fetch_result.get("emails", [])


@router.get("")
def get_dashboard(request: Request, refresh: bool = False):
    creds_dict = request.session.get("credentials")
    if not creds_dict:
        raise HTTPException(status_code=401, detail="Not authenticated")

    emails = _get_all_emails(creds_dict)
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}

    for email_obj in emails:
        p = calculate_priority(email_obj)
        counts[p["level"]] += 1

    return counts


@router.get("/stats")
def get_stats(request: Request, refresh: bool = False):
    creds_dict = request.session.get("credentials")
    if not creds_dict:
        raise HTTPException(status_code=401, detail="Not authenticated")

    emails = _get_all_emails(creds_dict)

    total = len(emails)
    high = 0
    medium = 0
    low = 0
    today_count = 0

    import email.utils
    from datetime import date, timezone
    today_date = date.today()

    for email_obj in emails:
        p = calculate_priority(email_obj)
        level = p["level"]
        if level == "HIGH":
            high += 1
        elif level == "MEDIUM":
            medium += 1
        else:
            low += 1

        # Count emails received today
        raw_date = email_obj.get("date", "")
        if raw_date:
            try:
                dt = email.utils.parsedate_to_datetime(raw_date)
                if dt.date() == today_date:
                    today_count += 1
            except Exception:
                pass

    log.info("/dashboard/stats | total=%d high=%d medium=%d low=%d today=%d",
             total, high, medium, low, today_count)

    return {
        "total": total,
        "high": high,
        "medium": medium,
        "low": low,
        "today": today_count,
    }