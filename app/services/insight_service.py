"""Dashboard aggregates (counts and stats)."""

from __future__ import annotations

import email.utils
from datetime import date
from typing import Dict, List, Optional

from app.core.exceptions import AuthException
from app.core.logging import get_logger
from app.services.classification_service import ClassificationService
from app.services import email_cache, watch_state
from app.services.gmail_service import fetch_emails, get_user_email

log = get_logger("insight_service", service_name="InsightService")


class InsightService:
    """Builds dashboard views from cache, watch state, or legacy Gmail fetch."""

    def __init__(self, classification_service: ClassificationService) -> None:
        self._classification = classification_service

    def _require_creds(self, creds_dict: Optional[dict]) -> dict:
        if not creds_dict:
            raise AuthException("Not authenticated", code="NOT_AUTHENTICATED", status_code=401)
        return creds_dict

    def _resolve_messages(self, creds_dict: dict) -> List[dict]:
        try:
            email_address = get_user_email(creds_dict)
        except Exception as e:
            log.warning("could not resolve user email: %s", e)
            email_address = None

        if email_address and watch_state.get(email_address):
            msgs = email_cache.list_all_messages(email_address)
            if msgs:
                return msgs

        fetch_result = fetch_emails(creds_dict, force_refresh=False)
        return fetch_result.get("emails", []) or []

    def dashboard_counts(self, creds_dict: Optional[dict]) -> Dict[str, int]:
        creds = self._require_creds(creds_dict)
        emails = self._resolve_messages(creds)
        counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        account_email = None
        try:
            account_email = get_user_email(creds)
        except Exception:
            pass
        for email_obj in emails:
            em = {**email_obj, "account_email": account_email} if account_email else email_obj
            p = self._classification.calculate_priority(em)
            counts[p["level"]] += 1
        return counts

    def dashboard_stats(self, creds_dict: Optional[dict]) -> Dict[str, int]:
        creds = self._require_creds(creds_dict)
        emails = self._resolve_messages(creds)
        total = len(emails)
        high = medium = low = today_count = 0
        today_date = date.today()
        account_email = None
        try:
            account_email = get_user_email(creds)
        except Exception:
            pass

        for email_obj in emails:
            em = {**email_obj, "account_email": account_email} if account_email else email_obj
            p = self._classification.calculate_priority(em)
            level = p["level"]
            if level == "HIGH":
                high += 1
            elif level == "MEDIUM":
                medium += 1
            else:
                low += 1

            raw_date = em.get("date", "")
            if raw_date:
                try:
                    dt = email.utils.parsedate_to_datetime(raw_date)
                    if dt.date() == today_date:
                        today_count += 1
                except (TypeError, ValueError, OverflowError):
                    pass

        log.info(
            "dashboard stats | total=%s high=%s medium=%s low=%s today=%s",
            total, high, medium, low, today_count,
        )
        return {
            "total": total,
            "high": high,
            "medium": medium,
            "low": low,
            "today": today_count,
        }
