import re
from typing import Dict, Any, List, Optional

from app.core.logging import get_logger
from app.repositories.bucket_repository import BucketRepository

log = get_logger("bucket_engine", service_name="BucketEngine")


# Helper for rule evaluation
def _evaluate_rule(rule: Dict[str, Any], email: Dict[str, Any]) -> bool:
    field = rule.get("field_name")
    operator = rule.get("operator")
    target_value = (rule.get("value") or "").lower()

    if not field or not operator:
        return False

    email_value = ""
    if field == "sender":
        email_value = email.get("sender_email") or email.get("sender") or ""
    elif field == "sender_domain":
        email_value = email.get("sender_domain") or ""
        if not email_value and "@" in (email.get("sender_email") or ""):
            email_value = (email.get("sender_email") or "").split("@")[1]
    elif field == "subject":
        email_value = email.get("subject") or ""
    elif field == "snippet":
        email_value = email.get("snippet") or ""
    elif field == "body":
        email_value = email.get("body") or ""
    elif field == "category":
        email_value = email.get("category") or email.get("ai_category") or ""
    elif field == "priority":
        email_value = email.get("level") or "LOW"

    email_value = str(email_value).lower()

    if operator == "equals":
        return email_value == target_value
    elif operator == "contains":
        return target_value in email_value
    elif operator == "starts_with":
        return email_value.startswith(target_value)
    elif operator == "ends_with":
        return email_value.endswith(target_value)
    elif operator == "domain_contains":
        return target_value in email_value
    elif operator == "regex":
        try:
            return bool(re.search(target_value, email_value, re.IGNORECASE))
        except re.error:
            return False

    return False


class BucketEngine:
    def __init__(self, bucket_repo: BucketRepository):
        self._repo = bucket_repo

    def get_system_bucket_map(self, user_id: str) -> Dict[str, str]:
        """Fetch all system buckets and return a mapping of name -> id."""
        buckets = self._repo.get_buckets(user_id)
        return {b["name"]: b["id"] for b in buckets if b.get("is_system")}

    def evaluate_system_rules(self, email: Dict[str, Any]) -> List[str]:
        """Returns a list of bucket names that match system rules."""
        matched = []
        subject = (email.get("subject") or "").lower()
        snippet = (email.get("snippet") or "").lower()
        full_text = subject + " " + snippet
        level = email.get("level") or "LOW"
        reasons = email.get("reasons") or []
        if any("vip" in str(r).lower() or "user marked as vip" in str(r).lower() for r in reasons):
            matched.append("VIP")

        # Escalations
        kw_escalation = r"\b(urgent|escalation|blocked|delay|overdue|complaint|failed|issue|risk|critical|immediate attention)\b"
        if re.search(kw_escalation, full_text, re.IGNORECASE):
            matched.append("Escalations")

        # Approvals
        kw_approval = r"\b(approve|approval|pending approval|review and approve|sign off|authorization|approved by)\b"
        if re.search(kw_approval, full_text, re.IGNORECASE):
            matched.append("Approvals")

        # Follow-ups
        kw_followup = r"\b(follow up|following up|reminder|gentle reminder|checking in|any update|awaiting response)\b"
        if re.search(kw_followup, full_text, re.IGNORECASE):
            matched.append("Follow-ups")

        # Finance
        kw_finance = r"\b(invoice|payment|billing|receipt|purchase order|PO|vendor|expense|reimbursement|statement|finance)\b"
        if re.search(kw_finance, full_text, re.IGNORECASE):
            matched.append("Finance")

        # Delegations
        kw_delegation = r"\b(delegate|assigned to|please handle|can you take this|forward to|ownership|action item)\b"
        if re.search(kw_delegation, full_text, re.IGNORECASE):
            matched.append("Delegations")

        # System Alerts
        kw_system = r"\b(github|supabase|vercel|netlify|render|server|api|build failed|deployment|error|exception|login alert|security alert|failed job)\b"
        if re.search(kw_system, full_text, re.IGNORECASE):
            matched.append("System Alerts")

        # Newsletters
        kw_news = r"\b(newsletter|digest|weekly update|monthly update|subscribe|subscription|roundup)\b"
        if re.search(kw_news, full_text, re.IGNORECASE):
            matched.append("Newsletters")

        # Promotions
        kw_promo = r"\b(offer|discount|sale|deal|coupon|promotion|limited time|marketing)\b"
        if re.search(kw_promo, full_text, re.IGNORECASE):
            matched.append("Promotions")

        # Social
        kw_social = r"\b(linkedin|facebook|instagram|twitter|x\.com|connection request|liked your post|commented on your post)\b"
        if re.search(kw_social, full_text, re.IGNORECASE):
            matched.append("Social")

        # Low Priority fallback if no other low prio matched
        if level == "LOW":
            matched.append("Low Priority")

        return matched

    def determine_bucket(
        self,
        email: Dict[str, Any],
        user_id: str,
        manual_assignments: Optional[Dict[str, Any]] = None,
        *,
        sys_bucket_map: Optional[Dict[str, str]] = None,
        bucket_id_to_name: Optional[Dict[str, str]] = None,
        active_rules: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Determines the primary bucket and bucket tags for an email.
        ``manual_assignments`` is a mapping of message_id -> assignment_record.

        When ``assign_emails_batch`` loads buckets/rules once, pass ``sys_bucket_map``,
        ``bucket_id_to_name``, and ``active_rules`` to avoid N Supabase round-trips per inbox page.
        """
        manual_assignments = manual_assignments or {}

        msg_id = str(email.get("id") or "")
        assignment = None
        if msg_id and msg_id in manual_assignments:
            assignment = manual_assignments[msg_id]

        if assignment and assignment.get("bucket_locked"):
            id_to_name = bucket_id_to_name
            if id_to_name is None:
                buckets = self._repo.get_buckets(user_id)
                id_to_name = {str(b["id"]): str(b["name"]) for b in buckets}
            b_name = id_to_name.get(str(assignment["bucket_id"]), "Unknown")

            return {
                "primary_bucket": b_name,
                "primary_bucket_id": assignment["bucket_id"],
                "bucket_tags": [b_name],
                "bucket_source": assignment.get("bucket_source", "MANUAL"),
                "bucket_locked": True,
                "bucket_reason": assignment.get("reason") or "Manual assignment",
            }

        sb_map = sys_bucket_map
        if sb_map is None:
            sb_map = self.get_system_bucket_map(user_id)

        rules_list = active_rules
        if rules_list is None:
            try:
                rules = self._repo.get_bucket_rules(user_id)
                rules_list = sorted(
                    [r for r in rules if r.get("is_active")],
                    key=lambda r: (-int(r.get("priority") or 0), str(r.get("created_at") or "")),
                )
            except Exception as e:
                log.warning("Failed to evaluate user rules: %s", e)
                rules_list = []

        for rule in rules_list:
            if _evaluate_rule(rule, email):
                b_name = rule.get("mail_buckets", {}).get("name", "Custom")
                return {
                    "primary_bucket": b_name,
                    "primary_bucket_id": rule["bucket_id"],
                    "bucket_tags": [b_name],
                    "bucket_source": "RULE",
                    "bucket_locked": False,
                    "bucket_reason": f"Matched rule: {rule['field_name']} {rule['operator']} {rule['value']}",
                }

        system_matches = self.evaluate_system_rules(email)

        if not system_matches:
            is_low = email.get("level") == "LOW"
            p_name = "Low Priority" if is_low else "Primary"
            return {
                "primary_bucket": p_name,
                "primary_bucket_id": sb_map.get(p_name),
                "bucket_tags": [p_name],
                "bucket_source": "SYSTEM",
                "bucket_locked": False,
                "bucket_reason": "Default fallback based on priority",
            }

        hierarchy = [
            "VIP",
            "Escalations",
            "Approvals",
            "Follow-ups",
            "Finance",
            "Delegations",
            "System Alerts",
            "Newsletters",
            "Promotions",
            "Social",
            "Low Priority",
            "Primary",
            "Other",
        ]

        primary = "Other"
        for h_bucket in hierarchy:
            if h_bucket in system_matches:
                primary = h_bucket
                break

        return {
            "primary_bucket": primary,
            "primary_bucket_id": sb_map.get(primary),
            "bucket_tags": list(set(system_matches + [primary])),
            "bucket_source": "SYSTEM",
            "bucket_locked": False,
            "bucket_reason": f"Matched keywords for: {', '.join(system_matches)}",
        }

    def assign_emails_batch(self, emails: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        """Assigns buckets to a batch of emails efficiently."""
        if not emails:
            return []
            
        msg_ids = [str(e["id"]) for e in emails if e.get("id")]
        assignments = []
        try:
            assignments = self._repo.get_email_bucket_assignments(user_id, msg_ids)
        except Exception as e:
            log.warning("Could not fetch manual assignments for batch: %s", e)

        assignment_map = {str(a["gmail_message_id"]): a for a in assignments}

        all_buckets: List[Dict[str, Any]] = []
        try:
            all_buckets = self._repo.get_buckets(user_id)
        except Exception as e:
            log.warning("Could not load buckets for batch assignment: %s", e)

        sys_bucket_map = {str(b["name"]): b["id"] for b in all_buckets if b.get("is_system")}
        bucket_id_to_name = {str(b["id"]): str(b["name"]) for b in all_buckets}

        active_rules: List[Dict[str, Any]] = []
        try:
            rules = self._repo.get_bucket_rules(user_id)
            active_rules = sorted(
                [r for r in rules if r.get("is_active")],
                key=lambda r: (-int(r.get("priority") or 0), str(r.get("created_at") or "")),
            )
        except Exception as e:
            log.warning("Could not load bucket rules for batch: %s", e)

        fallback = {
            "primary_bucket": "Primary",
            "primary_bucket_id": None,
            "bucket_tags": [],
            "bucket_source": "SYSTEM",
            "bucket_locked": False,
            "bucket_reason": "Default bucket assignment",
        }

        for email in emails:
            try:
                bucket_data = self.determine_bucket(
                    email,
                    user_id,
                    assignment_map,
                    sys_bucket_map=sys_bucket_map,
                    bucket_id_to_name=bucket_id_to_name,
                    active_rules=active_rules,
                )
                email.update(bucket_data)
            except Exception as e:
                log.warning("bucket assignment failed for %s: %s", email.get("id"), e)
                email.update(dict(fallback))

        return emails
