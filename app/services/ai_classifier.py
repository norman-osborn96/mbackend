import os
import re
import requests
import json
from threading import Lock

# Support multiple API keys for higher rate limits
_GEMINI_KEYS = [k.strip() for k in os.getenv("GEMINI_API_KEY", "").split(",") if k.strip()]
_key_index = 0
_key_lock = Lock()

def _call_gemini_api(url_path: str, payload: dict, timeout: int = 10):
    global _key_index
    
    # Refresh keys from env if possible, or use the cached ones
    # For simplicity, we use the ones parsed at startup, but we can re-parse if empty
    keys = _GEMINI_KEYS or [k.strip() for k in os.getenv("GEMINI_API_KEY", "").split(",") if k.strip()]
    
    if not keys:
        raise Exception("No GEMINI_API_KEY found in environment")
    
    last_response = None
    for attempt in range(len(keys)):
        with _key_lock:
            current_idx = _key_index % len(keys)
            key = keys[current_idx]
            _key_index += 1
            
        url = f"models/{url_path}:generateContent"
        full_url = f"https://generativelanguage.googleapis.com/v1beta/{url}?key={key}"
        
        try:
            res = requests.post(
                full_url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=timeout
            )
            last_response = res
            
            if res.status_code == 429:
                print(f"⚠️ Key {current_idx + 1}/{len(keys)} rate limited (429). Retrying...")
                continue
                
            return res
        except Exception as e:
            print(f"❌ API call error with key {current_idx + 1}/{len(keys)}: {e}")
            if attempt == len(keys) - 1:
                if last_response is not None: return last_response
                raise e
            continue
            
    return last_response


CACHE_FILE = os.path.join(os.path.dirname(__file__), "../ai_cache.json")
_cache_lock = Lock()

# Cache version — bump this string to invalidate all old entries
_CLASSIFY_VERSION = "exec_v3_action"
_SUMMARY_VERSION  = "summary_v7_complete"
_REPLY_VERSION    = "reply_v2"


# ─── Cache helpers ───────────────────────────────────────────────────────────

def _ensure_cache():
    if not os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)


def _load_cache():
    _ensure_cache()
    with _cache_lock:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def _save_cache(data):
    with _cache_lock:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def _make_classify_key(subject: str, snippet: str, sender_domain: str = "") -> str:
    base = f"{subject.strip().lower()}::{snippet.strip().lower()}::{sender_domain.strip().lower()}"
    return f"{_CLASSIFY_VERSION}::{base}"


def _make_summary_key(subject: str, snippet: str) -> str:
    base = f"{subject.strip().lower()}::{snippet.strip().lower()}"
    return f"{_SUMMARY_VERSION}::{base}"


def _make_reply_key(subject: str, snippet: str) -> str:
    base = f"{subject.strip().lower()}::{snippet.strip().lower()}"
    return f"{_REPLY_VERSION}::{base}"


def _strip_summary_preamble(text: str) -> str:
    """Remove meta phrases models sometimes emit before the actual summary."""
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
    """Cap length (~35–45 tokens / two sentences). Prefer ending on a full sentence."""
    if not text:
        return text
    words = text.strip().split()
    if len(words) <= max_words:
        return text.strip()
    chunk = " ".join(words[:max_words])
    # Prefer cutting after the last full sentence still inside the budget
    for sep in (". ", "? ", "! "):
        idx = chunk.rfind(sep)
        if idx >= 20:
            return chunk[: idx + 1].strip()
    return chunk.rstrip(".,;:") + "…"
# ─── Executive system prompt ─────────────────────────────────────────────────

_EXEC_SYSTEM_PROMPT = """You are a senior executive email triage assistant for a CEO, CFO, or CXO.
Your job is to classify each email into exactly one priority level using the strict criteria below.

HIGH priority — use when:
  • The executive must personally take action or make a decision
  • The matter is time-sensitive or involves important stakeholders (investors, board, clients)
  • Examples: board/investor meetings, M&A, regulatory deadlines, legal notices, 
    customer escalations, funding-round approvals, or urgent internal requests.

MEDIUM priority — the executive should be aware but does not need to act today:
  • Senior-leadership updates, escalated project issues, performance or budget reviews,
    partnership / vendor negotiations, HR decisions (hiring, terminations), MoU/LoI discussions,
    weekly/monthly reports from direct reports, meeting requests from important stakeholders.

LOW priority — everything else:
  • Newsletters, marketing, promotional offers, automated notifications, system alerts,
    no-reply or auto-generated emails, social media digests, FYI forwards, event invitations,
    software update notices, general team announcements not requiring executive input.

Also set "action":
  • "REQUIRES_REPLY" if the executive should personally reply, approve, confirm, or otherwise respond.
  • "FYI" if awareness suffices and no reply is expected.

Return ONLY valid JSON — no markdown, no extra text:
{"priority": "HIGH|MEDIUM|LOW", "reason": "one concise sentence", "confidence": 0.0-1.0, "action": "REQUIRES_REPLY" | "FYI"}

Confidence scoring:
  0.9–1.0 → very strong, unambiguous signals
  0.7–0.89 → clear but some ambiguity
  0.5–0.69 → moderate; could go either way
  0.3–0.49 → weak signals; best guess
"""


# ─── Main classification function ────────────────────────────────────────────

def classify_email_ai(subject: str, snippet: str, sender_domain: str = ""):
    try:
        cache = _load_cache()
        key = _make_classify_key(subject, snippet, sender_domain)

        if key in cache:
            print("⚡ CACHE HIT →", subject[:60])
            hit = dict(cache[key])
            hit.setdefault("action", "FYI")
            return hit

        print("🤖 AI CALL →", subject[:60])

        domain_context = f"\nSender domain: {sender_domain}" if sender_domain else ""

        user_prompt = f"""Classify this email for a CEO/CFO/CXO inbox.{domain_context}
Subject: {subject}
Content: {snippet}"""

        response = _call_gemini_api(
            "gemini-1.5-flash",
            {
                "system_instruction": {"parts": [{"text": _EXEC_SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": 0.05,
                    "maxOutputTokens": 120
                }
            },
            timeout=10
        )

        data = response.json()
        raw_output = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )

        print("🧠 RAW:", raw_output[:200])

        # Strip markdown fences if present
        cleaned = re.sub(r"```(?:json)?", "", raw_output).strip().strip("`").strip()

        try:
            parsed = json.loads(cleaned)
            priority = parsed.get("priority", "").upper()
            reason = parsed.get("reason", "")
            confidence = float(parsed.get("confidence", 0.6))
            action_raw = str(parsed.get("action") or "FYI").upper().replace(" ", "_")
            if action_raw in ("REQUIRES_REPLY", "ACTION_REQUIRED", "REPLY_REQUIRED"):
                action = "REQUIRES_REPLY"
            else:
                action = "FYI"

            # Clamp confidence
            confidence = max(0.1, min(1.0, confidence))

            if priority in ("HIGH", "MEDIUM", "LOW"):
                result = {
                    "level": priority,
                    "reason": reason or "AI classification",
                    "confidence": confidence,
                    "action": action,
                }
                cache[key] = result
                _save_cache(cache)
                return result

        except Exception:
            pass

        # Fallback: scan raw text for level keyword
        text = raw_output.upper()
        if "HIGH" in text:
            result = {"level": "HIGH", "reason": "AI detected executive urgency", "confidence": 0.55, "action": "FYI"}
        elif "MEDIUM" in text:
            result = {"level": "MEDIUM", "reason": "AI detected moderate importance", "confidence": 0.5, "action": "FYI"}
        else:
            result = {"level": "LOW", "reason": "AI detected low importance", "confidence": 0.45, "action": "FYI"}

        cache[key] = result
        _save_cache(cache)
        return result

    except Exception as e:
        print("❌ AI ERROR:", e)
        return {"level": "LOW", "reason": "AI unavailable — defaulting to low", "confidence": 0.2, "action": "FYI"}


# ─── Summary function ─────────────────────────────────────────────────────────

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


def summarize_email(subject: str, snippet: str):
    try:
        cache = _load_cache()
        key = _make_summary_key(subject, snippet)

        if key in cache:
            print("⚡ SUMMARY CACHE HIT →", subject[:60])
            return cache[key]

        print("📝 SUMMARY CALL →", subject[:60])

        user_prompt = f"""Subject: {subject}
Content: {snippet}

Write the grounded summary now: one or two complete sentences only, no preamble (only from the lines above)."""

        response = _call_gemini_api(
            "gemini-1.5-flash",
            {
                "system_instruction": {"parts": [{"text": _SUMMARY_SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": 0.05,
                    "maxOutputTokens": 220
                }
            },
            timeout=15
        )

        data = response.json()
        candidate = data.get("candidates", [{}])[0]
        summary = (
            candidate.get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )
        finish_reason = str(candidate.get("finishReason", ""))
        if finish_reason in ("length", "MAX_TOKENS"):
            print("⚠️ SUMMARY TRUNCATED (provider length limit) |", (subject or "")[:50])

        print("📝 SUMMARY:", summary[:120])

        if not summary:
            return None

        summary = _strip_summary_preamble(summary)
        if not summary:
            return None

        summary = _clamp_summary_words(summary, max_words=56)

        cache[key] = summary
        _save_cache(cache)
        return summary

    except Exception as e:
        print("❌ SUMMARY ERROR:", e)
        return None


# ─── Reply suggestion function ────────────────────────────────────────────────

def generate_reply_suggestion(subject: str, snippet: str, sender: str = "", tone: str = "professional"):
    try:
        cache = _load_cache()
        key = _make_reply_key(subject, snippet + "::" + tone)

        if key in cache:
            print("⚡ REPLY CACHE HIT →", subject[:60])
            return cache[key]

        print("✍️ REPLY CALL →", subject[:60], "| Tone:", tone)

        prompt = f"""Write a concise, {tone} reply to this email.
The reply should:
- Acknowledge the email's main point
- Provide a clear and relevant response
- Be {tone} in tone
- Be 2-4 sentences maximum
- NOT include a subject line, greeting or sign-off — just the reply body

From: {sender}
Subject: {subject}
Content: {snippet}"""

        response = _call_gemini_api(
            "gemini-1.5-flash",
            {
                "system_instruction": {"parts": [{"text": (
                    "You are a professional email assistant. "
                    "Write only the reply body — no subject, no greeting, no sign-off. "
                    f"Keep it concise (2-4 sentences), using a {tone} and natural tone."
                )}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.5,
                    "maxOutputTokens": 200
                }
            },
            timeout=15
        )

        if response.status_code != 200:
            print(f"❌ API ERROR: Status {response.status_code} | {response.text}")
            return None

        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            print(f"⚠️ NO CANDIDATES from Gemini: {data}")
            return None

        reply = (
            candidates[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )

        print("✍️ REPLY:", reply[:120])

        if not reply:
            return None

        cache[key] = reply
        _save_cache(cache)
        return reply

    except Exception as e:
        print("❌ REPLY ERROR:", e)
        return None
