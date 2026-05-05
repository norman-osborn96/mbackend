"""Gmail API service layer.

This module preserves the original `fetch_emails` / `send_reply` API used by
the legacy endpoints, and adds the primitives required for the push-based
pipeline:

* `start_watch` / `stop_watch`           - register / unregister Gmail push notifications
* `list_history`                         - incremental change log via `users.history.list`
* `get_message_full`                     - parse a single message into our envelope shape
* `get_user_email`                       - extract `emailAddress` from the user's profile
"""

import os
import re
import time
import base64
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

from app.core.logger import get_logger

log = get_logger("gmail_service")

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
]

# Pub/Sub topic Gmail will publish notifications to.
# Format: projects/<project-id>/topics/<topic-name>.
# In local-dev / mock setups this can be a placeholder; Gmail will reject
# `users.watch` if it isn't a real topic in your project, so we fall back to a
# clearly fake one and log a warning.
GMAIL_PUBSUB_TOPIC = os.getenv(
    "GMAIL_PUBSUB_TOPIC",
    "projects/mailpulse-dev/topics/gmail-notifications",
)

# Backwards-compatible legacy in-process cache used by `fetch_emails`.
EMAIL_CACHE = {"data": [], "last_fetch": 0}
CACHE_TTL = 60  # seconds — kept for the legacy endpoint only.


# ─── Body / payload parsing ──────────────────────────────────────────────────

def _ics_unfold(raw: str) -> str:
    lines: List[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line:
            continue
        if lines and (line[0] == " " or line[0] == "\t"):
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return "\n".join(lines)


def _iter_parts(payload: Optional[Dict[str, Any]]):
    if not payload:
        return
    yield payload
    for part in payload.get("parts") or []:
        yield from _iter_parts(part)


def _extract_calendar_bodies(payload: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for part in _iter_parts(payload):
        mt = (part.get("mimeType") or "").lower()
        if mt not in ("text/calendar", "application/ics", "application/ical"):
            continue
        data = part.get("body", {}).get("data")
        if not data:
            continue
        try:
            out.append(base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore"))
        except Exception:
            continue
    return out


def _split_ical_prop_line(line: str) -> Tuple[str, Dict[str, str]]:
    """Split 'DTSTART;TZID=X:VALUE' → (value, params)."""
    if ":" not in line:
        return "", {}
    head, value = line.split(":", 1)
    params: Dict[str, str] = {}
    parts = head.split(";")
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip()
    return value.strip(), params


def _parse_ics_dtstart_line(line: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (event_date 'YYYY-MM-DD', event_start_iso). At most one is set."""
    value, params = _split_ical_prop_line(line)
    if not value:
        return None, None

    if params.get("VALUE", "").upper() == "DATE":
        m = re.match(r"^(\d{8})", value)
        if m:
            d = m.group(1)
            return f"{d[0:4]}-{d[4:6]}-{d[6:8]}", None
        return None, None

    if re.match(r"^\d{8}$", value):
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}", None

    if "T" not in value:
        return None, None

    date_raw, time_raw_full = value.split("T", 1)
    if len(date_raw) != 8 or not date_raw.isdigit():
        return None, None

    time_remainder = time_raw_full
    if time_remainder.endswith("Z"):
        time_remainder = time_remainder[:-1]
        hmss = time_remainder.split(".")[0]
        if len(hmss) < 6:
            hmss = hmss.ljust(6, "0")
        try:
            dt = datetime.strptime(date_raw + hmss[:6], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return None, dt.isoformat().replace("+00:00", "Z")
        except ValueError:
            return None, None

    mo = re.match(r"^(\d{6})(?:\.\d+)?([+-]\d{4})$", time_remainder)
    if mo:
        hmss, off = mo.group(1), mo.group(2)
        sign = 1 if off[0] == "+" else -1
        oh, om = int(off[1:3]), int(off[3:5])
        delta = sign * timedelta(hours=oh, minutes=om)
        tz_off = timezone(delta)
        try:
            dt = datetime.strptime(date_raw + hmss, "%Y%m%d%H%M%S").replace(tzinfo=tz_off)
            return None, dt.isoformat()
        except ValueError:
            return None, None

    hmss = time_remainder.split(".")[0]
    if len(hmss) < 6:
        hmss = hmss.ljust(6, "0")
    tzid = params.get("TZID")
    if tzid:
        try:
            zi = ZoneInfo(tzid)
            dt = datetime.strptime(date_raw + hmss[:6], "%Y%m%d%H%M%S").replace(tzinfo=zi)
            return None, dt.isoformat()
        except Exception:
            pass
    try:
        dt = datetime.strptime(date_raw + hmss[:6], "%Y%m%d%H%M%S")
        return None, dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None, None


def _first_event_start_from_ics(ics: str) -> Dict[str, Any]:
    """Pull first DTSTART from the first VEVENT (calendar invite semantics)."""
    unfolded = _ics_unfold(ics)
    in_vevent = False
    for raw_line in unfolded.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "BEGIN:VEVENT":
            in_vevent = True
            continue
        if line == "END:VEVENT":
            in_vevent = False
            continue
        if not in_vevent or not line.startswith("DTSTART"):
            continue
        event_date, event_start_iso = _parse_ics_dtstart_line(line)
        if event_date:
            return {"event_date": event_date}
        if event_start_iso:
            return {"event_start_iso": event_start_iso}
    return {}


def _calendar_fields_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    for ics in _extract_calendar_bodies(payload):
        fields = _first_event_start_from_ics(ics)
        if fields:
            ev_iso = fields.get("event_start_iso")
            extra: Dict[str, Any] = dict(fields)
            if ev_iso:
                try:
                    dt = datetime.fromisoformat(ev_iso.replace("Z", "+00:00"))
                    if dt.tzinfo is not None:
                        extra["event_start_ms"] = int(dt.timestamp() * 1000)
                except Exception:
                    pass
            return extra
    return {}


def get_body(payload: Dict[str, Any]) -> str:
    if "parts" in payload:
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        for part in payload["parts"]:
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            nested = get_body(part)
            if nested:
                return nested
    else:
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return ""


def _parse_message(msg_data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw Gmail `messages.get` response to our flat envelope."""
    payload = msg_data.get("payload", {}) or {}
    headers = payload.get("headers", []) or []

    subject = sender = date_val = message_id_header = to_header = ""
    for h in headers:
        name = h.get("name", "")
        if name == "Subject":
            subject = h.get("value", "")
        elif name == "From":
            sender = h.get("value", "")
        elif name == "Date":
            date_val = h.get("value", "")
        elif name == "Message-ID":
            message_id_header = h.get("value", "")
        elif name == "To":
            to_header = h.get("value", "")

    envelope: Dict[str, Any] = {
        "id": msg_data.get("id", ""),
        "thread_id": msg_data.get("threadId", ""),
        "message_id_header": message_id_header,
        "subject": subject,
        "sender": sender,
        "to": to_header,
        "snippet": msg_data.get("snippet", ""),
        "body": get_body(payload),
        "date": date_val,
        "internal_date": int(msg_data.get("internalDate") or 0),
        "label_ids": msg_data.get("labelIds", []) or [],
        "history_id": msg_data.get("historyId", ""),
    }
    cal = _calendar_fields_from_payload(payload)
    if cal:
        envelope.update(cal)
    return envelope


# ─── Auth helper ─────────────────────────────────────────────────────────────

def get_gmail_service(creds_dict: Optional[Dict[str, Any]] = None):
    if not creds_dict:
        raise Exception("No credentials provided. User must be logged in.")

    creds = Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        token_uri=creds_dict.get("token_uri"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        scopes=creds_dict.get("scopes"),
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_user_email(creds_dict: Dict[str, Any]) -> str:
    """Return the authenticated user's Gmail address (used as cache key)."""
    service = get_gmail_service(creds_dict)
    profile = service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "")


# ─── Watch (push notification) primitives ────────────────────────────────────

def start_watch(creds_dict: Dict[str, Any], topic_name: Optional[str] = None) -> Dict[str, Any]:
    """Register a Gmail push-notification watch.

    Returns Gmail's `users.watch` response containing `historyId` and
    `expiration` (ms-since-epoch). The watch lasts ~7 days and must be renewed.
    """
    topic = topic_name or GMAIL_PUBSUB_TOPIC
    service = get_gmail_service(creds_dict)
    body = {
        "topicName": topic,
        # INBOX-only keeps notification volume sane; remove this line for
        # whole-mailbox change firehose.
        "labelIds": ["INBOX"],
        "labelFilterBehavior": "INCLUDE",
    }
    log.info("registering watch | topic=%s", topic)
    response = service.users().watch(userId="me", body=body).execute()
    log.info(
        "watch registered | historyId=%s | expiration=%s",
        response.get("historyId"),
        response.get("expiration"),
    )
    return response


def stop_watch(creds_dict: Dict[str, Any]) -> None:
    """Unregister the Gmail push-notification watch for this user."""
    service = get_gmail_service(creds_dict)
    try:
        service.users().stop(userId="me").execute()
        log.info("watch stopped")
    except HttpError as e:
        log.warning("watch stop returned %s — ignoring", e)


# ─── Incremental sync via history.list ───────────────────────────────────────

def list_history(
    creds_dict: Dict[str, Any],
    start_history_id: str,
    history_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Walk Gmail history since `start_history_id`, paginating until empty.

    Returns:
        {
          "added_ids":    [...],   # new INBOX message IDs
          "removed_ids":  [...],   # messages that left INBOX (deleted, archived)
          "updated_ids":  [...],   # label changes / mark-as-read
          "latest_history_id": "..."
        }
    """
    service = get_gmail_service(creds_dict)
    history_types = history_types or [
        "messageAdded",
        "messageDeleted",
        "labelAdded",
        "labelRemoved",
    ]

    added: set = set()
    removed: set = set()
    updated: set = set()
    page_token: Optional[str] = None
    latest_history_id = str(start_history_id)
    pages = 0

    while True:
        try:
            kwargs = {
                "userId": "me",
                "startHistoryId": str(start_history_id),
                "historyTypes": history_types,
                "labelId": "INBOX",
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.users().history().list(**kwargs).execute()
        except HttpError as e:
            # 404 means the historyId is too old — caller must do a full resync.
            if getattr(e.resp, "status", None) == 404:
                log.warning("history.list 404 — historyId %s expired", start_history_id)
                return {
                    "added_ids": [],
                    "removed_ids": [],
                    "updated_ids": [],
                    "latest_history_id": None,
                    "expired": True,
                }
            raise

        latest_history_id = resp.get("historyId", latest_history_id)
        for record in resp.get("history", []) or []:
            for added_msg in record.get("messagesAdded", []) or []:
                m = added_msg.get("message", {})
                if m.get("id"):
                    added.add(m["id"])
            for removed_msg in record.get("messagesDeleted", []) or []:
                m = removed_msg.get("message", {})
                if m.get("id"):
                    removed.add(m["id"])
            for label_evt in (record.get("labelsAdded", []) or []) + (record.get("labelsRemoved", []) or []):
                m = label_evt.get("message", {})
                if m.get("id"):
                    updated.add(m["id"])

        pages += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info(
        "history.list complete | start=%s | latest=%s | pages=%d | +%d -%d ~%d",
        start_history_id,
        latest_history_id,
        pages,
        len(added),
        len(removed),
        len(updated),
    )
    return {
        "added_ids": list(added),
        "removed_ids": list(removed),
        "updated_ids": list(updated - added),  # don't double-process new messages
        "latest_history_id": latest_history_id,
        "expired": False,
    }


# ─── Single-message fetch ────────────────────────────────────────────────────

def get_message_full(creds_dict: Dict[str, Any], message_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single message and return our parsed envelope. Returns None on
    404 (message was deleted between history fetch and now)."""
    service = get_gmail_service(creds_dict)
    try:
        raw = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except HttpError as e:
        if getattr(e.resp, "status", None) == 404:
            log.info("message %s gone (404) — skipping", message_id)
            return None
        raise
    return _parse_message(raw)


# ─── Legacy bulk fetch (kept for backwards compatibility) ────────────────────

def fetch_emails(creds_dict, force_refresh=False, limit=50, page_token=None):
    """Legacy bulk-list path used by `/dashboard` and the original `/emails`
    fallback. The push-based pipeline avoids this entirely.
    """
    try:
        current_time = time.time()

        if (
            not force_refresh
            and not page_token
            and EMAIL_CACHE.get("data")
            and current_time - EMAIL_CACHE.get("last_fetch", 0) < CACHE_TTL
        ):
            log.debug("legacy cache hit (fetch_emails)")
            return EMAIL_CACHE["data"]

        log.info("legacy fetch_emails | limit=%s pageToken=%s", limit, page_token)

        service = get_gmail_service(creds_dict)
        kwargs = {"userId": "me", "maxResults": limit}
        if page_token:
            kwargs["pageToken"] = page_token

        results = service.users().messages().list(**kwargs).execute()
        messages = results.get("messages", []) or []
        next_page_token = results.get("nextPageToken")

        emails: List[Dict[str, Any]] = []
        for msg in messages:
            try:
                raw = service.users().messages().get(
                    userId="me", id=msg["id"], format="full"
                ).execute()
                emails.append(_parse_message(raw))
            except HttpError as e:
                log.warning("messages.get failed for %s: %s", msg.get("id"), e)

        result_data = {"emails": emails, "next_page_token": next_page_token}

        if not page_token:
            EMAIL_CACHE["data"] = result_data
            EMAIL_CACHE["last_fetch"] = current_time

        return result_data

    except Exception as e:
        log.error("Gmail API failed, using fallback data: %s", e)
        return {
            "emails": [
                {"id": "1", "subject": "Urgent Warning: Server downtime",
                 "sender": "sysadmin@company.com",
                 "snippet": "Server will be down for maintenance tonight..."},
                {"id": "2", "subject": "Weekly Newsletter",
                 "sender": "news@newsletter.com",
                 "snippet": "Here are this week's highlights..."},
            ],
            "next_page_token": None,
        }


def _email_from_from_header(sender_header: str) -> str:
    s = (sender_header or "").strip()
    if "<" in s and ">" in s:
        return s[s.find("<") + 1 : s.find(">")].strip().lower()
    return s.lower()


def thread_user_has_message_after(
    creds_dict: Dict[str, Any],
    thread_id: str,
    user_email: str,
    after_internal_ms: int,
) -> bool:
    """True if the Gmail thread contains any message from user_email after after_internal_ms (ms, exclusive)."""
    user_l = (user_email or "").strip().lower()
    if not thread_id or not user_l:
        return False
    try:
        service = get_gmail_service(creds_dict)
        t = service.users().threads().get(
            userId="me",
            id=thread_id,
            format="metadata",
            metadataHeaders=["From"],
        ).execute()
    except HttpError as e:
        log.warning("threads.get failed for %s: %s", thread_id, e)
        return False

    for m in t.get("messages", []) or []:
        try:
            mid = int(m.get("internalDate") or 0)
        except (TypeError, ValueError):
            continue
        if mid <= after_internal_ms:
            continue
        headers = (m.get("payload") or {}).get("headers") or []
        from_val = ""
        for h in headers:
            if h.get("name") == "From":
                from_val = h.get("value", "")
                break
        if _email_from_from_header(from_val) == user_l:
            return True
    return False


# ─── Send reply (unchanged) ──────────────────────────────────────────────────

def send_reply(
    creds_dict: dict,
    to: str,
    subject: str,
    body: str,
    thread_id: str = "",
    reply_to_message_id: str = "",
) -> dict:
    """Send a reply email via Gmail API."""
    service = get_gmail_service(creds_dict)

    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = reply_subject
    if reply_to_message_id:
        msg["In-Reply-To"] = reply_to_message_id
        msg["References"] = reply_to_message_id

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    body_payload = {"raw": raw}
    if thread_id:
        body_payload["threadId"] = thread_id

    return service.users().messages().send(userId="me", body=body_payload).execute()
