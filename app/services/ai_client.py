"""MailPulse executive AI: OpenRouter classification, CXO profiles, daily budget, batching, rules."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

import requests

from app.core.config import Settings
from app.core.exceptions import AIClassificationException, RateLimitException
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.repositories.sender_repository import SenderRepository

log = get_logger("ai_client", service_name="MailPulseAI")

# ── OpenRouter ──────────────────────────────────────────────────────────────
# Model IDs are loaded from Settings (.env). Use https://openrouter.ai/api/v1/models (filter :free) — stale slugs return HTTP 404.

DAILY_AI_CALL_LIMIT = 45
PROFILE_MAX_AGE_DAYS = 7
"""Legacy default chunk size; prefetch uses ``Settings.ai_classification_batch_size``."""
BATCH_CHUNK_SIZE = 20

_MAX_HTTP_ATTEMPTS = 3


def _retry_after_seconds(resp: requests.Response, default: float = 2.5) -> float:
    """Parse provider ``Retry-After`` (seconds). Cap to avoid blocking workers too long."""
    h = (resp.headers.get("Retry-After") or "").strip()
    if not h:
        return default
    try:
        return min(60.0, max(1.0, float(h)))
    except ValueError:
        return default


# Admin persona (saved in user_ai_profiles.profile["admin_persona"]); drives classification prompt.
ALLOWED_PERSONA_FIELDS: frozenset = frozenset(
    {
        "finance",
        "support",
        "tech",
        "developer",
        "operations",
        "sales",
        "hr",
        "legal",
        "executive",
        "other",
    }
)

PERSONA_FIELD_LABELS: Dict[str, str] = {
    "finance": "finance and accounting",
    "support": "customer support and success",
    "tech": "technology and IT",
    "developer": "software engineering and development",
    "operations": "operations and logistics",
    "sales": "sales and business development",
    "hr": "human resources and people",
    "legal": "legal and compliance",
    "executive": "general management and leadership",
    "other": "general professional context",
}


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _midnight_utc_iso() -> str:
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.isoformat()


def _strip_code_fences(raw: str) -> str:
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t).strip()
    return t.strip("`").strip()


def _strip_llm_thinking_and_noise(raw: str) -> str:
    """Remove common reasoning wrappers so JSON extraction succeeds."""
    t = (raw or "").strip()
    for name in ("think", "redacted_thinking", "reasoning", "analysis"):
        o, c = "<" + name + ">", "<" + "/" + name + ">"
        t = re.sub(re.escape(o) + r"[\s\S]*?" + re.escape(c), "", t, flags=re.IGNORECASE)
    t = re.sub(r"^\s*#+\s*Thought[^\n]*\n", "", t, flags=re.IGNORECASE)
    return t.strip()


def _iter_balanced_json_objects(t: str) -> List[dict]:
    """Parse every top-level `{...}` substring that is valid JSON object."""
    out: List[dict] = []
    n = len(t)
    i = 0
    while i < n:
        if t[i] != "{":
            i += 1
            continue
        depth = 0
        start = i
        for j in range(i, n):
            c = t[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    chunk = t[start : j + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            out.append(obj)
                    except json.JSONDecodeError:
                        pass
                    i = j + 1
                    break
        else:
            i += 1
    return out


def _extract_json_object(text: str) -> Optional[dict]:
    """Parse a JSON object from model output (handles fences, thinking, trailing text)."""
    t = _strip_llm_thinking_and_noise(_strip_code_fences(text))
    try:
        o = json.loads(t)
        return o if isinstance(o, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    candidates = _iter_balanced_json_objects(t)
    for obj in reversed(candidates):
        if any(k in obj for k in ("priority", "why", "category", "suggested_action")):
            return obj
    for obj in reversed(candidates):
        if "role" in obj and "industry" in obj:
            return obj
    if candidates:
        return candidates[-1]
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            o = json.loads(m.group(0))
            return o if isinstance(o, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _extract_json_array(text: str) -> Optional[list]:
    t = _strip_llm_thinking_and_noise(_strip_code_fences(text))
    try:
        out = json.loads(t)
        return out if isinstance(out, list) else None
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\[[\s\S]*\]", t)
    if m:
        try:
            out = json.loads(m.group(0))
            return out if isinstance(out, list) else None
        except json.JSONDecodeError:
            return None
    return None


def _extract_classification_batch_results(text: str) -> Optional[List[dict]]:
    """Parse ``{\"results\": [ {...}, ... ]}`` from model output."""
    t = _strip_llm_thinking_and_noise(_strip_code_fences(text))
    try:
        o = json.loads(t)
        if isinstance(o, dict) and isinstance(o.get("results"), list):
            return [x for x in o["results"] if isinstance(x, dict)]
    except (json.JSONDecodeError, TypeError):
        pass
    for obj in reversed(_iter_balanced_json_objects(t)):
        r = obj.get("results")
        if isinstance(r, list):
            return [x for x in r if isinstance(x, dict)]
    m = re.search(r"\{[\s\S]*\"results\"\s*:[\s\S]*\}", t)
    if m:
        try:
            o = json.loads(m.group(0))
            if isinstance(o, dict) and isinstance(o.get("results"), list):
                return [x for x in o["results"] if isinstance(x, dict)]
        except json.JSONDecodeError:
            return None
    return None


# ── Daily call budget (JSON file, per account, resets UTC midnight) ─────────


class DailyAIBudgetStore:
    """Tracks OpenRouter calls per Gmail account per UTC day (file-backed)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        if not self._path.exists():
            return {"users": {}}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {"users": {}}
        except (OSError, json.JSONDecodeError, TypeError) as e:
            log.warning("ai budget read failed: %s", e)
            return {"users": {}}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self._path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        Path(tmp).replace(self._path)

    def get_state(self, account_email: str) -> Tuple[int, str]:
        """Return (calls_today, utc_date_str_for_bucket)."""
        key = (account_email or "").strip().lower()
        if not key:
            return 0, _utc_today()
        today = _utc_today()
        with self._lock:
            root = self._read()
            users = root.setdefault("users", {})
            row = users.get(key) or {}
            if row.get("date") != today:
                return 0, today
            return int(row.get("calls", 0) or 0), today

    def increment(self, account_email: str, delta: int = 1) -> int:
        key = (account_email or "").strip().lower()
        if not key or delta <= 0:
            return 0
        today = _utc_today()
        with self._lock:
            root = self._read()
            users = root.setdefault("users", {})
            row = users.get(key) or {}
            if row.get("date") != today:
                row = {"date": today, "calls": 0}
            row["calls"] = int(row.get("calls", 0) or 0) + delta
            row["date"] = today
            users[key] = row
            self._write(root)
            return int(row["calls"])

    def snapshot(self, account_email: str) -> dict:
        used, _ = self.get_state(account_email)
        return {
            "used": used,
            "limit": DAILY_AI_CALL_LIMIT,
            "resets_at": _midnight_utc_iso(),
        }


# ── HTTP client ───────────────────────────────────────────────────────────────


class AIClient:
    """OpenRouter chat completions with retries, model override, and primary→fallback."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._timeout = float(settings.openrouter_timeout_seconds)

    def chat_completion(
        self,
        *,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        model: Optional[str] = None,
        classification_batch: bool = False,
    ) -> Dict[str, Any]:
        key = self._settings.openrouter_api_key
        if not key or not key.get_secret_value().strip():
            raise AIClassificationException(
                "OpenRouter API key is not configured",
                code="AI_NOT_CONFIGURED",
                status_code=503,
            )

        url = self._settings.openrouter_base_url
        headers = {
            "Authorization": f"Bearer {key.get_secret_value()}",
            "Content-Type": "application/json",
            "HTTP-Referer": self._settings.openrouter_http_referer,
            "X-Title": self._settings.openrouter_app_title,
        }

        order = [model or self._settings.openrouter_primary_model, self._settings.openrouter_fallback_model]
        models_to_try: List[str] = []
        seen: set[str] = set()
        for m in order:
            if m and m not in seen:
                seen.add(m)
                models_to_try.append(m)

        last_error: Optional[Exception] = None
        max_attempts = 1 if classification_batch else _MAX_HTTP_ATTEMPTS
        extra_429_slot = 0 if classification_batch else 1
        for model_id in models_to_try:
            body = {
                "model": model_id,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            used_429_backoff = False
            attempt = 0
            max_tries = max_attempts + extra_429_slot
            while attempt < max_tries:
                attempt += 1
                try:
                    resp = requests.post(url, headers=headers, json=body, timeout=self._timeout)
                    if resp.status_code == 429:
                        if classification_batch:
                            raise RateLimitException("OpenRouter rate limit")
                        if not used_429_backoff:
                            used_429_backoff = True
                            wait = _retry_after_seconds(resp, default=3.0)
                            log.warning(
                                "OpenRouter rate limit | model=%s — waiting %.1fs then one extra attempt",
                                model_id,
                                wait,
                            )
                            time.sleep(wait)
                            attempt -= 1
                            continue
                        last_error = RateLimitException("OpenRouter rate limit")
                        break
                    if resp.status_code >= 400:
                        raise AIClassificationException(
                            f"OpenRouter HTTP {resp.status_code}",
                            details={"body": resp.text[:500], "model": model_id},
                        )
                    log.debug("OpenRouter ok | model=%s", model_id)
                    return resp.json()
                except RateLimitException as e:
                    last_error = e
                    if classification_batch:
                        log.warning(
                            "OpenRouter rate limit during batch classification | model=%s — no secondary model retry",
                            model_id,
                        )
                        raise
                    break
                except AIClassificationException as e:
                    last_error = e
                    break
                except requests.Timeout as e:
                    last_error = e
                    log.warning("OpenRouter timeout | model=%s attempt=%s", model_id, attempt)
                except requests.RequestException as e:
                    last_error = e
                    log.warning("OpenRouter request error | model=%s attempt=%s: %s", model_id, attempt, e)

                if attempt < max_tries:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
            log.warning("OpenRouter giving up on model=%s: %s", model_id, last_error)

        if isinstance(last_error, RateLimitException):
            log.warning(
                "OpenRouter failed all models (rate limited). Use rule/local fallbacks where available. last=%s",
                last_error,
            )
        else:
            log.error("OpenRouter failed all models: %s", last_error)
        raise AIClassificationException(
            "OpenRouter request failed after retries",
            details={"error": str(last_error) if last_error else ""},
        ) from last_error


# ── Rule-based fallback (ordered rules) ───────────────────────────────────────


_PROMO_RE = re.compile(
    r"\b(promo|promotion|sale|%\s*off|discount|limited\s*time|"
    r"special\s*offer|newsletter|unsubscribe|view\s+in\s+browser|"
    r"marketing\s+email|advertisement)\b",
    re.IGNORECASE,
)


def rule_based_classify(
    subject: str,
    snippet: str,
    sender_domain: str = "",
    *,
    sender_email: str = "",
    manual_vip: Optional[Sequence[str]] = None,
    profile: Optional[dict] = None,
    own_company_domain: str = "",
) -> Dict[str, Any]:
    """Deterministic triage when AI is off, budget exceeded, or parse failed."""
    text = f"{subject} {snippet}".lower()
    sender_e = (sender_email or "").strip().lower()
    domain = (sender_domain or "").strip().lower()
    own_co = (own_company_domain or "").strip().lower()

    vip_set = {e.strip().lower() for e in (manual_vip or []) if e}
    if profile:
        for e in profile.get("vip_senders") or []:
            if isinstance(e, str):
                vip_set.add(e.strip().lower())

    if _PROMO_RE.search(text) or _PROMO_RE.search(sender_e):
        return {
            "level": "LOW",
            "reason": "Rule-based: promotional or marketing content",
            "confidence": 0.55,
            "action": "FYI",
            "category": "noise",
            "suggested_action": "ignore",
            "llm_used": False,
        }

    if sender_e in vip_set:
        return {
            "level": "HIGH",
            "reason": "Rule-based: VIP sender",
            "confidence": 0.7,
            "action": "REQUIRES_REPLY",
            "category": "relationship",
            "suggested_action": "reply_now",
            "llm_used": False,
        }

    urgent = re.search(
        r"\b(urgent|asap|today|deadline|board|investor)\b",
        f"{subject} {snippet}",
        re.IGNORECASE,
    )
    if urgent:
        return {
            "level": "HIGH",
            "reason": "Rule-based: urgency / board / investor signal in subject or body",
            "confidence": 0.62,
            "action": "REQUIRES_REPLY",
            "category": "action_required",
            "suggested_action": "reply_now",
            "llm_used": False,
        }

    if own_co and domain == own_co:
        return {
            "level": "MEDIUM",
            "reason": "Rule-based: internal company domain",
            "confidence": 0.5,
            "action": "FYI",
            "category": "fyi",
            "suggested_action": "delegate",
            "llm_used": False,
        }

    if re.search(r"\bunsubscribe\b", text, re.IGNORECASE):
        return {
            "level": "LOW",
            "reason": "Rule-based: mailing-list / unsubscribe",
            "confidence": 0.6,
            "action": "FYI",
            "category": "noise",
            "suggested_action": "archive",
            "llm_used": False,
        }

    return {
        "level": "LOW",
        "reason": "Rule-based: default low priority",
        "confidence": 0.4,
        "action": "FYI",
        "category": "noise",
        "suggested_action": "ignore",
        "llm_used": False,
    }


def _map_priority_to_level(p: str) -> str:
    p = (p or "").strip().lower()
    if p == "critical":
        return "HIGH"
    if p == "high":
        return "HIGH"
    if p == "medium":
        return "MEDIUM"
    return "LOW"


def _map_suggested_to_action(s: str) -> str:
    s = (s or "").strip().lower()
    if s == "reply_now":
        return "REQUIRES_REPLY"
    return "FYI"


def _normalize_ai_class_dict(parsed: dict) -> Dict[str, Any]:
    return {
        "level": _map_priority_to_level(str(parsed.get("priority", ""))),
        "reason": str(parsed.get("why") or parsed.get("reason") or "AI classification")[:500],
        "confidence": max(0.1, min(1.0, float(parsed.get("confidence", 0.65)))),
        "action": _map_suggested_to_action(str(parsed.get("suggested_action", ""))),
        "category": str(parsed.get("category") or ""),
        "suggested_action": str(parsed.get("suggested_action") or ""),
        "reply_urgency_hours": parsed.get("reply_urgency_hours"),
        "llm_used": True,
    }


def _normalize_batch_email_result(parsed: dict) -> Dict[str, Any]:
    """Map batch API row (priority + reason only) to full classifier dict."""
    raw_pri = str(parsed.get("priority") or "").strip()
    lvl = raw_pri.upper() if raw_pri.upper() in ("HIGH", "MEDIUM", "LOW") else _map_priority_to_level(raw_pri)
    if lvl not in ("HIGH", "MEDIUM", "LOW"):
        lvl = "LOW"
    reason = str(parsed.get("reason") or parsed.get("why") or "AI classification").strip() or "AI classification"
    reason = reason[:500]
    confidence = 0.75 if lvl == "HIGH" else 0.62 if lvl == "MEDIUM" else 0.52
    if lvl == "HIGH":
        action = "REQUIRES_REPLY"
        suggested = "reply_now"
    else:
        action = "FYI"
        suggested = "delegate" if lvl == "MEDIUM" else "ignore"
    return {
        "level": lvl,
        "reason": reason,
        "confidence": confidence,
        "action": action,
        "category": "",
        "suggested_action": suggested,
        "reply_urgency_hours": None,
        "llm_used": True,
    }


def _admin_persona_dict(profile: Optional[dict]) -> dict:
    if not isinstance(profile, dict):
        return {}
    ap = profile.get("admin_persona")
    return ap if isinstance(ap, dict) else {}


def admin_persona_complete(ap: Any) -> bool:
    if not isinstance(ap, dict):
        return False
    designation = str(ap.get("designation") or "").strip()
    field = str(ap.get("field") or "").strip().lower()
    return bool(designation) and field in ALLOWED_PERSONA_FIELDS


def profile_sufficient_for_llm(profile: Optional[dict]) -> bool:
    """True when OpenRouter classification may run (admin persona or legacy inferred role+industry)."""
    if not isinstance(profile, dict) or not profile:
        return False
    if admin_persona_complete(_admin_persona_dict(profile)):
        return True
    role = str(profile.get("role") or "").strip()
    industry = str(profile.get("industry") or "").strip()
    return bool(role and industry)


def effective_profile_for_classification(profile: Optional[dict], settings: Settings) -> Optional[dict]:
    """If OpenRouter is configured but Supabase has no usable profile, merge minimal role/industry so LLM can run."""
    if isinstance(profile, dict) and profile_sufficient_for_llm(profile):
        return profile
    k = settings.openrouter_api_key
    if not k or not str(k.get_secret_value() or "").strip():
        return profile if isinstance(profile, dict) else None
    base = {"role": "professional", "industry": "general business"}
    if isinstance(profile, dict) and profile:
        merged = dict(base)
        for key, val in profile.items():
            if val is None:
                continue
            if isinstance(val, str) and not val.strip():
                continue
            merged[key] = val
        return merged
    return dict(base)


def build_classification_system_prompt(profile: dict) -> str:
    ap = _admin_persona_dict(profile)
    vip = profile.get("vip_senders") or []
    action_topics = profile.get("action_topics") or []
    ignore_topics = profile.get("ignore_topics") or []
    vip_list = ", ".join(str(x) for x in vip[:12]) if vip else "(none inferred yet)"
    act = ", ".join(str(x) for x in action_topics[:8]) if action_topics else "(none)"
    ign = ", ".join(str(x) for x in ignore_topics[:8]) if ignore_topics else "(none)"

    if admin_persona_complete(ap):
        designation = str(ap.get("designation") or "").strip()
        field_key = str(ap.get("field") or "").strip().lower()
        field_label = PERSONA_FIELD_LABELS.get(field_key, field_key)
        who_line = (
            f"You are an executive assistant AI supporting a {designation} "
            f"working in {field_label}."
        )
    else:
        role = profile.get("role") or "executive"
        industry = profile.get("industry") or "general business"
        who_line = f"You are an executive assistant AI for a {role} in {industry}."

    focus_notes = str(ap.get("focus_notes") or "").strip()
    notes_block = f"\nUser context (from settings): {focus_notes}\n" if focus_notes else ""

    return f"""{who_line}
{notes_block}
VIP senders (always critical): {vip_list}
Always action-required topics: {act}
Always ignore topics: {ign}

Classify this email. Think step by step silently, then return ONLY valid JSON:
{{
  "priority": "critical|high|medium|low",
  "category": "action_required|decision_needed|fyi|relationship|noise",
  "why": "one sentence max, executive language",
  "suggested_action": "reply_now|delegate|schedule|archive|ignore",
  "reply_urgency_hours": null,
  "confidence": 0.75
}}

Rules:
- Board members / investors = never below high
- Anything with deadline today or tomorrow = critical
- Marketing / newsletters = noise unless from known VIP
- Internal from direct report asking for decision = action_required
- FWD chains older than 3 days with no question = low
- Promotional or sales email = low unless from VIP sender
"""


PROFILE_INFER_SYSTEM = """You analyze a CXO's inbox sample and infer how they work.
Return ONLY valid JSON (no markdown):
{
  "role": "CEO|CFO|CTO|COO|CMO|other",
  "industry": "short label e.g. fintech, saas, manufacturing, healthcare, other",
  "vip_senders": ["email@domain.com", ... up to 10],
  "action_topics": ["short phrase", ... exactly 5],
  "ignore_topics": ["short phrase", ... exactly 5],
  "own_company_domain": "domain.com or empty if unknown"
}
Infer VIPs as board, investors, key customers, direct reports based on From addresses and subjects."""


def _email_samples_for_profile(emails: List[dict], limit: int = 20) -> str:
    lines: List[str] = []
    for i, em in enumerate(emails[:limit]):
        subj = str(em.get("subject") or "")[:200]
        snip = str(em.get("snippet") or em.get("body") or "")[:400]
        snd = str(em.get("sender") or "")[:120]
        lines.append(f"--- Email {i+1} ---\nFrom: {snd}\nSubject: {subj}\nSnippet: {snip}")
    return "\n".join(lines)


def _infer_profile_via_ai(settings: Settings, client: AIClient, sample_emails: List[dict]) -> dict:
    user_block = _email_samples_for_profile(sample_emails, 20)
    last_exc: Optional[Exception] = None
    for model_id in (settings.openrouter_primary_model, settings.openrouter_fallback_model):
        try:
            data = client.chat_completion(
                messages=[
                    {"role": "system", "content": PROFILE_INFER_SYSTEM},
                    {"role": "user", "content": f"Infer the CXO profile from these emails:\n\n{user_block}"},
                ],
                temperature=0.1,
                max_tokens=700,
                model=model_id,
            )
            raw = str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
            parsed = _extract_json_object(raw)
            if not parsed:
                raise AIClassificationException("Profile inference returned invalid JSON")
            out = {
                "role": str(parsed.get("role") or "other"),
                "industry": str(parsed.get("industry") or "other"),
                "vip_senders": [str(x).lower() for x in (parsed.get("vip_senders") or []) if x][:10],
                "action_topics": [str(x) for x in (parsed.get("action_topics") or [])][:5],
                "ignore_topics": [str(x) for x in (parsed.get("ignore_topics") or [])][:5],
                "own_company_domain": str(parsed.get("own_company_domain") or "").lower().strip(),
                "inferred_at": datetime.now(timezone.utc).isoformat(),
            }
            while len(out["action_topics"]) < 5:
                out["action_topics"].append("general approvals")
            while len(out["ignore_topics"]) < 5:
                out["ignore_topics"].append("newsletters and bulk mail")
            return out
        except (AIClassificationException, RateLimitException) as e:
            last_exc = e
            continue
        except Exception as e:
            last_exc = e
            continue
    raise AIClassificationException(
        "Profile inference failed",
        details={"error": str(last_exc) if last_exc else ""},
    ) from last_exc


class MailPulseAIEngine:
    """CXO profile lifecycle, budgeting, batch + single OpenRouter classification."""

    def __init__(
        self,
        settings: Settings,
        client: AIClient,
        sender_repository: "SenderRepository",
    ) -> None:
        self._settings = settings
        self._client = client
        self._repo = sender_repository
        self._budget = DailyAIBudgetStore(settings.ai_daily_budget_path)

    def get_ai_budget(self, account_email: str) -> dict:
        return self._budget.snapshot(account_email)

    def _budget_allows_ai(self, account_email: Optional[str]) -> bool:
        if not account_email:
            return True
        used, _ = self._budget.get_state(account_email)
        return used < DAILY_AI_CALL_LIMIT

    def _charge_ai_call(self, account_email: Optional[str]) -> None:
        if account_email:
            self._budget.increment(account_email, 1)

    def load_profile(self, account_email: str) -> Optional[dict]:
        if not account_email:
            return None
        return self._repo.get_user_ai_profile(account_email)

    def profile_is_stale(self, profile: dict) -> bool:
        raw = profile.get("inferred_at") or profile.get("updated_at")
        if not raw:
            return True
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - dt > timedelta(days=PROFILE_MAX_AGE_DAYS)
        except (TypeError, ValueError):
            return True

    def maybe_refresh_profile(
        self,
        account_email: str,
        inbox_sample: List[dict],
        *,
        force: bool = False,
    ) -> Optional[dict]:
        """Infer and persist CXO profile when missing or stale; consumes one AI call when run."""
        if not account_email or not inbox_sample:
            return self.load_profile(account_email)

        existing = self.load_profile(account_email)
        if existing and not force and not self.profile_is_stale(existing):
            return existing

        if not self._budget_allows_ai(account_email):
            log.warning("profile refresh skipped — daily AI budget exhausted | %s", account_email)
            return existing

        try:
            inferred = _infer_profile_via_ai(self._settings, self._client, inbox_sample)
            merged = dict(inferred)
            if isinstance(existing, dict) and "admin_persona" in existing:
                merged["admin_persona"] = existing["admin_persona"]
            self._repo.upsert_user_ai_profile(account_email, merged)
            self._charge_ai_call(account_email)
            return merged
        except AIClassificationException as e:
            log.warning("profile inference failed: %s", e)
            return existing
        except Exception as e:
            log.exception("profile inference error: %s", e)
            return existing

    def classify_one(
        self,
        *,
        subject: str,
        snippet: str,
        sender_domain: str,
        sender_email: str,
        account_email: Optional[str],
        profile: Optional[dict],
        manual_vip: Sequence[str],
    ) -> Dict[str, Any]:
        """Single-email classification; may return needs_profile_setup."""
        raw_profile = profile if isinstance(profile, dict) else None
        prof_work = effective_profile_for_classification(raw_profile, self._settings)
        own_co = (prof_work or {}).get("own_company_domain") or ""
        prof_dict = prof_work

        if not profile_sufficient_for_llm(prof_work):
            rb = rule_based_classify(
                subject,
                snippet,
                sender_domain,
                sender_email=sender_email,
                manual_vip=manual_vip,
                profile=prof_dict,
                own_company_domain=own_co,
            )
            if account_email and not profile_sufficient_for_llm(raw_profile):
                return {**rb, "needs_profile_setup": True}
            return rb

        if not self._budget_allows_ai(account_email):
            return rule_based_classify(
                subject,
                snippet,
                sender_domain,
                sender_email=sender_email,
                manual_vip=manual_vip,
                profile=prof_dict,
                own_company_domain=own_co,
            )

        system = build_classification_system_prompt(prof_dict or {})
        user = f"Subject: {subject}\nFrom: {sender_email}\nDomain: {sender_domain}\nBody snippet: {snippet}"

        raw_preview = ""
        for model_id in (self._settings.openrouter_primary_model, self._settings.openrouter_fallback_model):
            if not model_id:
                continue
            try:
                data = self._client.chat_completion(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.05,
                    max_tokens=220,
                    model=model_id,
                )
                raw = str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
                raw_preview = raw[:400]
                parsed = _extract_json_object(raw)
                if parsed:
                    self._charge_ai_call(account_email)
                    return _normalize_ai_class_dict(parsed)
            except (AIClassificationException, RateLimitException) as e:
                log.warning("single classify AI failed | model=%s err=%s", model_id, e)
            except Exception as e:
                log.warning("single classify error | model=%s err=%s", model_id, e)

        log.warning(
            "single classify: no JSON from OpenRouter (using rule fallback) | prefix=%r",
            raw_preview[:220],
        )
        return rule_based_classify(
            subject,
            snippet,
            sender_domain,
            sender_email=sender_email,
            manual_vip=manual_vip,
            profile=prof_dict,
            own_company_domain=own_co,
        )

    def classify_batch(
        self,
        *,
        account_email: Optional[str],
        profile: Optional[dict],
        manual_vip: Sequence[str],
        items: List[Tuple[str, str, str, str, str, str]],
    ) -> List[Dict[str, Any]]:
        """Batch-classify emails in one OpenRouter call when possible.

        Each item: (email_id, subject, snippet, sender_domain, sender_email, received_at).
        Returns rows in the same order as ``items``.
        """
        if not items:
            return []

        raw_profile = profile if isinstance(profile, dict) else None
        prof_work = effective_profile_for_classification(raw_profile, self._settings)
        own_co = (prof_work or {}).get("own_company_domain") or ""
        prof_dict = prof_work

        def _fallback_row(s: str, sn: str, d: str, e: str) -> Dict[str, Any]:
            rb = rule_based_classify(
                s,
                sn,
                d,
                sender_email=e,
                manual_vip=manual_vip,
                profile=prof_dict,
                own_company_domain=own_co,
            )
            if account_email and not profile_sufficient_for_llm(raw_profile):
                return {**rb, "needs_profile_setup": True}
            return rb

        if not profile_sufficient_for_llm(prof_work):
            log.info(
                "batch classification rule-only | emails=%s reason=no_llm_profile batch_path=prefetch",
                len(items),
            )
            return [_fallback_row(s, sn, d, e) for _, s, sn, d, e, _ in items]

        if not self._budget_allows_ai(account_email):
            log.info(
                "batch classification rule-only | emails=%s reason=daily_ai_budget batch_path=prefetch",
                len(items),
            )
            return [_fallback_row(s, sn, d, e) for _, s, sn, d, e, _ in items]

        system = build_classification_system_prompt(prof_dict or {})
        payload: List[Dict[str, Any]] = []
        for email_id, s, sn, d, e, recv in items:
            eid = str(email_id or "").strip()
            payload.append(
                {
                    "email_id": eid or str(len(payload)),
                    "sender": (e or "")[:320],
                    "subject": (s or "")[:900],
                    "snippet": (sn or "")[:2000],
                    "received_at": (recv or "")[:80],
                }
            )

        user = (
            "You will receive a JSON array EMAILS of email objects. Each has: "
            "email_id, sender, subject, snippet, received_at.\n"
            "Classify EACH email for the executive described in your system instructions.\n"
            "Return ONLY valid JSON (no markdown) with this exact shape:\n"
            '{"results":[{"email_id":"<must match input>","priority":"HIGH|MEDIUM|LOW","reason":"short reason"}]}\n'
            "Rules for priority values: use only the strings HIGH, MEDIUM, or LOW (uppercase).\n"
            "Include one results entry per email; email_id must match the input.\n\n"
            f"EMAILS:\n{json.dumps(payload, ensure_ascii=False)}"
        )

        est_max = max(800, min(6000, 180 + 95 * len(items)))

        def _rows_from_rule(reason: str) -> List[Dict[str, Any]]:
            log.warning("batch classification rule fallback for whole chunk | emails=%s detail=%s", len(items), reason)
            return [_fallback_row(s, sn, d, e) for _, s, sn, d, e, _ in items]

        for model_id in (self._settings.openrouter_primary_model, self._settings.openrouter_fallback_model):
            if not model_id:
                continue
            try:
                data = self._client.chat_completion(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.05,
                    max_tokens=est_max,
                    model=model_id,
                    classification_batch=True,
                )
                raw = str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
                parsed_rows = _extract_classification_batch_results(raw)
                if not parsed_rows:
                    log.warning(
                        "batch classification parse failed | model=%s emails=%s preview=%r",
                        model_id,
                        len(items),
                        raw[:400],
                    )
                    continue

                by_id: Dict[str, Dict[str, Any]] = {}
                for row in parsed_rows:
                    k = str(row.get("email_id") or "").strip()
                    if not k:
                        continue
                    by_id[k] = _normalize_batch_email_result(row)

                out: List[Dict[str, Any]] = []
                for email_id, s, sn, d, e, _ in items:
                    kid = str(email_id or "").strip()
                    if kid and kid in by_id:
                        out.append(dict(by_id[kid]))
                    else:
                        log.warning(
                            "batch classification missing AI row | email_id=%s using rule fallback",
                            kid or "?",
                        )
                        out.append(_fallback_row(s, sn, d, e))

                self._charge_ai_call(account_email)
                log.info(
                    "OpenRouter batch classification ok | model=%s emails=%s openrouter_calls=1",
                    model_id,
                    len(items),
                )
                return out
            except RateLimitException as e:
                log.warning(
                    "OpenRouter rate limit — rule fallback for batch | emails=%s err=%s",
                    len(items),
                    e,
                )
                return _rows_from_rule("rate_limit_429")
            except AIClassificationException as batch_err:
                log.warning(
                    "batch classification OpenRouter failed | model=%s emails=%s err=%s",
                    model_id,
                    len(items),
                    batch_err,
                )
                continue
            except Exception as e:
                log.warning("batch classification error | model=%s emails=%s err=%s", model_id, len(items), e)
                continue

        return _rows_from_rule("openrouter_exhausted_or_failed")


def chat_for_summary_reply(
    settings: Settings,
    client: AIClient,
    *,
    messages: List[Dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> Dict[str, Any]:
    """Summaries / reply drafts use the same primary → fallback chain as classification (env-configured)."""
    return client.chat_completion(
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        model=None,
    )
