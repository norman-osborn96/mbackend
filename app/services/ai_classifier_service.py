"""AI classification, summaries, and reply drafts — delegates intelligence to ``ai_client``."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import Settings
from app.core.exceptions import AIClassificationException
from app.core.logging import get_logger
from app.repositories.sender_repository import SenderRepository
from app.services.ai_cache import AICache
from app.services.ai_client import (
    AIClient,
    MailPulseAIEngine,
    chat_for_summary_reply,
    effective_profile_for_classification,
    profile_sufficient_for_llm,
)
from app.services.priority_engine import extract_email

log = get_logger("ai_classifier_service", service_name="AIClassifierService")

_CLASSIFY_VERSION = "cxo_v3"
_SUMMARY_VERSION = "summary_v8_cxo"
_REPLY_VERSION = "reply_v4"

_SUMMARY_SYSTEM_PROMPT = """You rewrite email text into a concise neutral inbox summary.

STRICT RULES (must follow all):
• Use ONLY facts that appear verbatim or clearly in the Subject and Content below. Do NOT add names, dates, amounts, deadlines, risks, actions, or consequences that are not in that text.
• Do NOT infer, guess, generalize, or fill gaps. If something is not stated, omit it.
• Do NOT mention "consequences of ignoring" unless the email itself states them.
• Output ONLY the summary text. Start with the first sentence immediately — no preamble, labels, apologies, or phrases like "Here is", "Summary:", or "Below is".
• Write exactly two complete sentences when Content has usable detail; otherwise one complete sentence. Each sentence must be grammatically complete (do not cut off mid-phrase). Aim for about 35–45 tokens total (roughly 26–55 words).
• When Content is not empty, the summary MUST include at least one concrete detail from Content—do not merely repeat or lightly rephrase the Subject alone.
• No bullet points, no headers, no quotes.
• If Content is empty or useless, paraphrase only what the Subject states in one short complete sentence."""


def _strip_summary_preamble(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    patterns = [
        r"^(here is|here's|below is)\s+(a\s+)?(concise\s+)?(neutral\s+)?(inbox\s+)?(summary|overview)\s*[:-]\s*",
        r"^(summary|inbox summary)\s*[:-]\s*",
        r"^(the following is|this (email|message))\s+(a\s+)?summary\s*[:-]\s*",
    ]
    for p in patterns:
        nxt = re.sub(p, "", t, flags=re.IGNORECASE | re.DOTALL).strip()
        if nxt and nxt != t:
            t = nxt
    return t


def _clamp_summary_words(text: str, max_words: int = 56) -> str:
    if not text:
        return text
    words = text.strip().split()
    if len(words) <= max_words:
        return text.strip()
    chunk = " ".join(words[:max_words])
    for sep in (". ", "? ", "! "):
        idx = chunk.rfind(sep)
        if idx >= 20:
            return chunk[: idx + 1].strip()
    return chunk.rstrip(".,;:") + "…"


def _make_classify_key(
    subject: str,
    snippet: str,
    sender_domain: str,
    account_email: str = "",
) -> str:
    base = f"{subject.strip().lower()}::{snippet.strip().lower()}::{sender_domain.strip().lower()}::{account_email.strip().lower()}"
    return f"{_CLASSIFY_VERSION}::{base}"


def _make_summary_key(subject: str, snippet: str) -> str:
    base = f"{subject.strip().lower()}::{snippet.strip().lower()}"
    return f"{_SUMMARY_VERSION}::{base}"


def _make_reply_key(subject: str, snippet: str, sender_email: str = "") -> str:
    se = extract_email(sender_email).strip().lower()
    base = f"{subject.strip().lower()}::{snippet.strip().lower()}::{se}"
    return f"{_REPLY_VERSION}::{base}"


class AIClassifierService:
    """CXO-aware classification (OpenRouter + profile + budget) plus summaries and reply drafts."""

    def __init__(
        self,
        settings: Settings,
        cache: AICache,
        client: AIClient,
        sender_repository: SenderRepository,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._client = client
        self._repo = sender_repository
        self._engine = MailPulseAIEngine(settings, client, sender_repository)
        self._prof_cache: dict[str, tuple[float, Optional[dict]]] = {}
        self._manual_vip_cache: tuple[float, List[str]] | None = None
        self._manual_vip_ttl_s = 45.0

    def get_ai_budget(self, account_email: str) -> dict:
        return self._engine.get_ai_budget(account_email)

    def get_profile(self, account_email: Optional[str]) -> Optional[dict]:
        if not account_email:
            return None
        now = time.monotonic()
        hit = self._prof_cache.get(account_email.lower())
        if hit and now - hit[0] < 45.0:
            return hit[1]
        prof = self._engine.load_profile(account_email)
        self._prof_cache[account_email.lower()] = (now, prof)
        return prof

    def invalidate_profile_cache(self, account_email: str) -> None:
        self._prof_cache.pop(account_email.lower(), None)

    def _cached_manual_vip_list(self) -> List[str]:
        now = time.monotonic()
        hit = self._manual_vip_cache
        if hit is not None and now - hit[0] < self._manual_vip_ttl_s:
            return hit[1]
        try:
            lst = list(self._repo.list_vip_emails())
        except Exception as e:
            log.debug("manual vip list unavailable: %s", e)
            lst = []
        self._manual_vip_cache = (now, lst)
        return lst

    def maybe_warm_cxo_profile(self, account_email: str, inbox_sample: List[dict], *, force: bool = False) -> None:
        if not account_email or not inbox_sample:
            return
        self._engine.maybe_refresh_profile(account_email, inbox_sample, force=force)
        self.invalidate_profile_cache(account_email)

    def build_ai_prefetch(self, account_email: str, emails: List[dict]) -> Dict[str, Dict[str, Any]]:
        """Batch OpenRouter classification keyed by Gmail message id.

        Uses ``AI_CLASSIFICATION_BATCH_SIZE`` (default 20) emails per HTTP request.
        Skips OpenRouter for messages already present in the AI cache.
        """
        if not account_email or not emails:
            return {}
        profile = self.get_profile(account_email)
        prof_e = effective_profile_for_classification(profile, self._settings)
        if not prof_e or not profile_sufficient_for_llm(prof_e):
            return {}
        manual_vip = self._cached_manual_vip_list()
        out: Dict[str, Dict[str, Any]] = {}

        def _domain(addr: str) -> str:
            a = (addr or "").strip().lower()
            if "@" in a:
                return a.rsplit("@", 1)[-1]
            return ""

        batch_size = max(1, min(64, int(self._settings.ai_classification_batch_size)))
        pending: List[Tuple[str, str, str, str, str, str]] = []

        with_id = 0
        for em in emails:
            eid = str(em.get("id") or "").strip()
            if not eid:
                continue
            with_id += 1
            se = extract_email(str(em.get("sender") or ""))
            sd = _domain(se)
            subj = str(em.get("subject") or "")
            snip = str(em.get("snippet") or "")
            received = str(em.get("date") or em.get("received_at") or "").strip()
            if not received:
                raw_internal = em.get("internalDate")
                if raw_internal is None:
                    raw_internal = em.get("internal_date")
                if raw_internal is not None:
                    try:
                        ms = int(raw_internal)
                        received = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
                    except (TypeError, ValueError, OSError):
                        received = ""

            cache_key = _make_classify_key(subj, snip, sd, account_email)
            cached = self._cache.get(cache_key)
            if isinstance(cached, dict) and str(cached.get("level") or "") in ("HIGH", "MEDIUM", "LOW"):
                row = dict(cached)
                row.setdefault("action", "FYI")
                if row.get("llm_used") is None:
                    r = str(row.get("reason") or "")
                    row["llm_used"] = not r.lower().startswith("rule-based")
                out[eid] = row
                continue

            pending.append((eid, subj, snip, sd, se, received))

        log.info(
            "AI batch prefetch | account=%s emails_in=%s with_message_id=%s cache_hits=%s pending_llm=%s batch_size=%s",
            account_email,
            len(emails),
            with_id,
            len(out),
            len(pending),
            batch_size,
        )

        if not pending:
            return out

        planned_http = (len(pending) + batch_size - 1) // batch_size
        log.info(
            "AI batch prefetch OpenRouter | uncached=%s planned_requests_upper_bound=%s (actual may be lower if rule-only)",
            len(pending),
            planned_http,
        )

        for start in range(0, len(pending), batch_size):
            chunk = pending[start : start + batch_size]
            rows = self._engine.classify_batch(
                account_email=account_email,
                profile=prof_e,
                manual_vip=manual_vip,
                items=chunk,
            )
            for i, row in enumerate(rows):
                if i >= len(chunk):
                    break
                eid, subj, snip, sd, _, _ = chunk[i]
                out[eid] = row
                ck = _make_classify_key(subj, snip, sd, account_email)
                self._cache.set(ck, {k: v for k, v in row.items() if k != "needs_profile_setup"})

        return out

    def classify_email_ai(
        self,
        subject: str,
        snippet: str,
        sender_domain: str = "",
        *,
        sender_email: str = "",
        account_email: Optional[str] = None,
        profile: Optional[dict] = None,
    ) -> Dict[str, Any]:
        prof = profile if profile is not None else self.get_profile(account_email or "")
        key = _make_classify_key(subject, snippet, sender_domain, account_email or "")
        cached = self._cache.get(key)
        if cached is not None and isinstance(cached, dict):
            hit = dict(cached)
            hit.setdefault("action", "FYI")
            if hit.get("llm_used") is None:
                r = str(hit.get("reason") or "")
                hit["llm_used"] = not r.lower().startswith("rule-based")
            return hit

        manual_vip = self._cached_manual_vip_list()

        result = self._engine.classify_one(
            subject=subject,
            snippet=snippet,
            sender_domain=sender_domain,
            sender_email=sender_email or "",
            account_email=account_email,
            profile=prof,
            manual_vip=manual_vip,
        )
        self._cache.set(key, {k: v for k, v in result.items() if k != "needs_profile_setup"})
        return result

    def summarize_email(self, subject: str, snippet: str) -> Optional[str]:
        key = _make_summary_key(subject, snippet)
        cached = self._cache.get(key)
        if cached is not None:
            return str(cached) if cached else None

        user_prompt = f"""Subject: {subject}
Content: {snippet}

Write the grounded summary now: one or two complete sentences only, no preamble (only from the lines above)."""

        try:
            data = chat_for_summary_reply(
                self._settings,
                self._client,
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.05,
                max_tokens=220,
            )
            choice = data.get("choices", [{}])[0]
            summary = str(choice.get("message", {}).get("content", "")).strip()
            finish_reason = choice.get("finish_reason", "")
            if finish_reason == "length":
                log.warning("summary truncated by provider | subject=%s", (subject or "")[:50])
            if not summary:
                return None
            summary = _strip_summary_preamble(summary)
            if not summary:
                return None
            summary = _clamp_summary_words(summary, max_words=56)
            self._cache.set(key, summary)
            return summary
        except AIClassificationException as e:
            log.warning("summarize fallback (none): %s", e)
            return None

    def generate_reply_suggestion(
        self,
        subject: str,
        snippet: str,
        sender: str = "",
        *,
        regenerate: bool = False,
    ) -> str:
        key = _make_reply_key(subject, snippet, sender)
        if not regenerate:
            cached = self._cache.get(key)
            if cached is not None and isinstance(cached, str) and cached.strip():
                return cached

        prompt = f"""Write a concise, professional reply to this email.
The reply should:
- Acknowledge the email's main point
- Provide a clear and relevant response
- Be professional but natural in tone
- Be 2-4 sentences maximum
- NOT include a subject line, greeting or sign-off — just the reply body

From: {sender}
Subject: {subject}
Content: {snippet}"""

        fallback = (
            "Thank you for your message. I have received it and will review the details "
            "and follow up with you shortly."
        )
        try:
            variation_hint = (
                "\n\nRewrite with different wording than a generic acknowledgment; "
                "stay specific to this email."
                if regenerate
                else ""
            )
            temperature = 0.72 if regenerate else 0.5
            data = chat_for_summary_reply(
                self._settings,
                self._client,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a professional email assistant. "
                            "Write only the reply body — no subject, no greeting, no sign-off. "
                            "Keep it concise (2-4 sentences), professional and natural."
                        ),
                    },
                    {"role": "user", "content": prompt + variation_hint},
                ],
                temperature=temperature,
                max_tokens=200,
            )
            reply = (
                str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
            )
            if reply:
                self._cache.set(key, reply)
                return reply
        except AIClassificationException as e:
            log.warning("reply suggestion AI unavailable: %s", e)
        self._cache.set(key, fallback)
        return fallback
