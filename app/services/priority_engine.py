import re
from app.services.supabase_client import supabase
from app.services.manual_rules_service import apply_rules
from app.services.ai_classifier import classify_email_ai, summarize_email


# ─── Helpers ─────────────────────────────────────────────────────────────────

def extract_email(sender_value):
    sender_value = (sender_value or "").strip()
    if "<" in sender_value and ">" in sender_value:
        start = sender_value.find("<") + 1
        end = sender_value.find(">")
        return sender_value[start:end].strip().lower()
    return sender_value.lower()


def _extract_domain(email_addr: str) -> str:
    parts = email_addr.split("@")
    return parts[1].strip().lower() if len(parts) == 2 else ""


def _summary_source(email: dict) -> tuple:
    """Original subject + text for AI summary: snippet, extended with body when snippet is short."""
    subj = email.get("subject") or ""
    snip = (email.get("snippet") or "").strip()
    body = (email.get("body") or "").strip()
    if not body:
        return subj, snip
    flat = re.sub(r"\s+", " ", body)[:2000]
    if len(snip) < 140:
        return subj, (f"{snip} {flat}".strip() if snip else flat)
    return subj, snip


# ─── Supabase loaders / savers ────────────────────────────────────────────────

def load_vip_senders():
    try:
        res = supabase.table("vip_senders").select("email").execute()
        return [row["email"].strip().lower() for row in (res.data or [])]
    except Exception as e:
        print("VIP LOAD ERROR:", e)
        return []


def load_medium_senders():
    try:
        res = supabase.table("medium_priority_senders").select("email").execute()
        return [row["email"].strip().lower() for row in (res.data or [])]
    except Exception as e:
        print("MEDIUM LOAD ERROR:", e)
        return []


def save_vip_sender(sender):
    sender = extract_email(sender)
    try:
        supabase.table("medium_priority_senders").delete().eq("email", sender).execute()
        supabase.table("vip_senders").upsert({"email": sender}).execute()
        return True
    except Exception as e:
        print("VIP SAVE ERROR:", e)
        return False


def save_medium_sender(sender):
    sender = extract_email(sender)
    try:
        supabase.table("vip_senders").delete().eq("email", sender).execute()
        supabase.table("medium_priority_senders").upsert({"email": sender}).execute()
        return True
    except Exception as e:
        print("MEDIUM SAVE ERROR:", e)
        return False


def remove_vip_sender(sender):
    sender = extract_email(sender)
    try:
        supabase.table("vip_senders").delete().eq("email", sender).execute()
        return True
    except Exception as e:
        print("VIP REMOVE ERROR:", e)
        return False


def remove_medium_sender(sender):
    sender = extract_email(sender)
    try:
        supabase.table("medium_priority_senders").delete().eq("email", sender).execute()
        return True
    except Exception as e:
        print("MEDIUM REMOVE ERROR:", e)
        return False


# ─── Signal pattern definitions ───────────────────────────────────────────────
#
# Each entry: (regex_pattern, score_delta, human_reason)
# Patterns are matched against the combined subject + snippet (lowercase).

# Domains whose emails are unconditionally critical (government / regulators)
_CRITICAL_SENDER_DOMAINS = {
    "sec.gov", "irs.gov", "rbi.org.in", "sebi.gov.in", "mca.gov.in",
    "nclt.gov.in", "enforcement.gov.in", "cbdt.gov.in", "mof.gov.in",
    "courts.gov", "judiciary.gov.in",
}

# Very strong executive signals — each alone can push toward HIGH
_HIGH_SIGNALS = [
    (r"\bboard\s*(meeting|approval|resolution|vote|agenda)\b",                  45, "Board communication"),
    (r"\b(investor|shareholder|stakeholder)\s*(call|meeting|update|concern|approval)\b", 40, "Investor communication"),
    (r"\b(acquisition|merger|m&a|due diligence|term\s*sheet)\b",                45, "M&A activity"),
    (r"\b(ipo|fundraising|funding\s*round|series\s+[a-e]|valuation)\b",         45, "Funding / IPO"),
    (r"\b(statutory\s*audit|internal\s*audit|auditor|audit\s*finding)\b",       40, "Audit matter"),
    (r"\b(regulatory|compliance\s*deadline|sec\s*filing|sebi|rbi|irs|nclt|mca)\b", 40, "Regulatory / compliance"),
    (r"\b(legal\s*notice|lawsuit|litigation|court\s*order|arbitration|injunction|summons)\b", 50, "Legal action"),
    (r"\b(data\s*breach|security\s*incident|cyber\s*attack|ransomware|hack(ed|ing)?)\b", 50, "Security incident"),
    (r"\b(contract.{0,20}(sign|execut|approv|review|terminat|expir))\b",        40, "Contract action required"),
    (r"\b(q[1-4]\s*(result|earning|shortfall|miss|beat)|annual\s*report|earnings\s*call)\b", 35, "Financial reporting"),
    (r"\b(revenue.{0,25}(miss|target|shortfall|decline|alert|risk))\b",         40, "Revenue alert"),
    (r"\b(crisis|production\s*down|system\s*outage|critical\s*failure|emergency)\b", 50, "Crisis / outage"),
    (r"\b(capex\s*approval|budget\s*approv|p&l\s*review|profit.{0,10}loss)\b",  35, "Financial approval"),
    (r"\b(restructur|retrenchment|mass\s*layoff|workforce\s*reduction)\b",       35, "Org restructuring"),
    (r"\bimmediate\s*action\s*required\b|\baction\s*required\b|\burgent\s*action\b", 35, "Immediate action required"),
    (r"\b(penalty|fine|sanction|enforcement\s*action)\b",                        40, "Regulatory penalty"),
    (r"\b(whistleblow|fraud\s*allegation|investigation\s*open)\b",               45, "Fraud / investigation"),
    (r"\b(key\s*man|key\s*person|founder\s*(depart|resign|leav))\b",            40, "Key person departure"),
    (r"\b(force\s*majeure|material\s*breach|default\s*notice)\b",               45, "Contractual default"),
    (r"\b(hostile\s*takeover|activist\s*investor|proxy\s*fight)\b",             50, "Hostile action"),
]

# Moderate executive signals — contribute to MEDIUM
_MEDIUM_SIGNALS = [
    (r"\b(weekly|monthly|quarterly)\s*(report|update|summary|review)\b",         15, "Periodic report"),
    (r"\b(performance\s*review|appraisal|360\s*feedback|kpi\s*review)\b",        20, "Performance review"),
    (r"\b(partnership|mou|letter\s*of\s*intent|loi|collaboration)\b",            25, "Partnership discussion"),
    (r"\b(offer\s*letter|hiring|onboarding|resignation|exit\s*interview)\b",     18, "HR matter"),
    (r"\b(vendor|supplier|rfp|rfq|proposal|tender)\b",                           15, "Vendor matter"),
    (r"\b(project.{0,15}(update|status|milestone|delay|risk))\b",                15, "Project update"),
    (r"\b(approval\s*needed|sign.?off\s*needed|awaiting\s*approval)\b",          22, "Approval needed"),
    (r"\b(escalat(ed|ion)|raised\s*by|raised\s*an\s*issue|flagged)\b",           22, "Escalation"),
    (r"\b(budget\s*request|opex\s*request|resource\s*request)\b",                18, "Resource request"),
    (r"\b(client\s*complaint|customer\s*escalation|churn\s*risk)\b",             20, "Customer issue"),
    (r"\b(strategic\s*plan|roadmap\s*(review|update)|annual\s*plan)\b",          18, "Strategic planning"),
    (r"\b(board\s*pack|pre-read|pre\s*read|briefing\s*doc)\b",                   20, "Board prep material"),
]

# Signals that strongly indicate automated / bulk / promotional email → push DOWN
_BULK_SIGNALS = [
    (r"\b(unsubscribe|opt[\s-]?out|mailing\s*list|email\s*preference)\b",       -40, "Bulk mail / unsubscribe link"),
    (r"\b(newsletter|digest|roundup|weekly\s*wrap|monthly\s*wrap)\b",            -35, "Newsletter"),
    (r"\b(promo(tion(al)?)?|discount|special\s*offer|limited\s*offer|coupon|deal|sale|% off)\b", -40, "Promotional"),
    (r"\b(no[\s\-]?reply|do[\s\-]?not[\s\-]?reply|donotreply)\b",              -30, "No-reply / automated"),
    (r"\b(system\s*(generated|alert|notif)|auto[\s-]?(generated|notif|email|alert))\b", -25, "System generated"),
    (r"\b(linkedin\s*connection|twitter|facebook\s*notification|instagram)\b",  -30, "Social media notification"),
    (r"\b(app\s*(update|version|release)|software\s*update|patch\s*note)\b",    -25, "Software update"),
    (r"\b(your\s*(order|delivery|shipment|package|receipt|subscription))\b",    -30, "E-commerce / delivery"),
    (r"\b(verify\s*your\s*email|confirm\s*your\s*(email|account|address))\b",   -15, "Account verification"),
    (r"\b(security\s*code|otp|one[\s-]?time\s*(password|code|pin))\b",          -20, "OTP / 2FA code"),
    (r"\b(survey|feedback\s*form|rate\s*your\s*experience|nps)\b",              -15, "Survey / feedback request"),
    (r"\b(webinar\s*(invite|registration)|virtual\s*event|free\s*trial)\b",     -20, "Marketing event"),
    (r"\b(payment\s*receipt|invoice\s*(attached|generated|ready)|billing\s*statement)\b", -10, "Automated billing"),
]

# Temporal urgency — adds score on top of existing signals
_TEMPORAL_SIGNALS = [
    (r"\b(today|by\s*eod|end\s*of\s*(day|business)|tonight|this\s*morning|right\s*now|immediately|asap)\b", 20, "Same-day urgency"),
    (r"\b(by\s*tomorrow|within\s*24\s*hours?|next\s*24\s*hours?)\b",             15, "Next-day urgency"),
    (r"\b(this\s*week|by\s*friday|end\s*of\s*week|eow)\b",                       8, "This-week urgency"),
    (r"\b(overdue|past\s*due|missed\s*deadline|late\s*filing|late\s*payment)\b", 25, "Overdue / missed deadline"),
    (r"\b(deadline|due\s*(date|on|by)|expires?\s*(on|in)|expiry\s*date)\b",      10, "Deadline mentioned"),
]


def _apply_patterns(text: str, patterns: list) -> tuple:
    total = 0
    reasons = []
    for pattern, delta, label in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            total += delta
            reasons.append(label)
    return total, reasons


def _is_automated_sender(sender_email: str) -> bool:
    return bool(re.search(
        r"(noreply|no[\-\.]reply|mailer|notifications?|alerts?|donotreply|auto[\-\.]?mail"
        r"|system@|bounce@|postmaster@|support\+auto|daemon@)",
        sender_email,
        re.IGNORECASE
    ))


# ─── Score thresholds ─────────────────────────────────────────────────────────
#
# Raising thresholds compared to the old engine (was 60/30) so HIGH is harder to
# reach from keywords alone and the AI prompt's stricter criteria are respected.
_SCORE_HIGH   = 60   # Lowered from 70
_SCORE_MEDIUM = 30   # Lowered from 35
_BULK_CUTOFF  = -25  # stays same

# AI confidence floor: if AI says HIGH but confidence is below this, downgrade to MEDIUM
_HIGH_CONFIDENCE_FLOOR = 0.72


# ─── Main priority logic ──────────────────────────────────────────────────────

def calculate_priority(email: dict) -> dict:
    score = 0
    reasons: list = []

    sender_raw = email.get("sender") or ""
    subject    = (email.get("subject") or "").lower()
    snippet    = (email.get("snippet") or "").lower()
    full_text  = subject + " " + snippet

    sender_email  = extract_email(sender_raw)
    sender_domain = _extract_domain(sender_email)

    VIP_SENDERS    = load_vip_senders()
    MEDIUM_SENDERS = load_medium_senders()

    # ── Helpers for final return ──
    def _finalize(lvl, sc, reas, conf=None, summ=None, act="FYI"):
        # Always try to get a summary if missing
        if summ is None:
            try:
                s_subj, s_content = _summary_source(email)
                summ = summarize_email(s_subj, s_content)
            except Exception:
                summ = None
        
        res = {
            "level": lvl,
            "score": sc,
            "reasons": reas,
            "sender_email": sender_email,
            "summary": summ,
            "ai_action": act
        }
        if conf is not None:
            res["confidence"] = conf
        return res

    # ── 1. User overrides (strongest) ────────────────────────────────────────
    if sender_email in VIP_SENDERS:
        return _finalize("HIGH", 100, ["User marked as VIP"])

    if sender_email in MEDIUM_SENDERS:
        return _finalize("MEDIUM", 50, ["User marked as Medium Priority"])

    # ── 2. Admin rule engine ─────────────────────────────────────────────────
    rule_result = apply_rules(email)
    if rule_result:
        return _finalize(
            rule_result["level"],
            rule_result["score"],
            rule_result["reasons"]
        )

    # ── 3. Critical-domain sender (government / regulator) ───────────────────
    if sender_domain in _CRITICAL_SENDER_DOMAINS:
        score += 55
        reasons.append(f"Critical regulatory sender ({sender_domain})")

    # Automated sender address → push down
    if _is_automated_sender(sender_email):
        score -= 22
        reasons.append("Automated / no-reply sender address")

    # ── 4. Multi-dimensional keyword scoring ─────────────────────────────────
    h_score, h_reasons = _apply_patterns(full_text, _HIGH_SIGNALS)
    m_score, m_reasons = _apply_patterns(full_text, _MEDIUM_SIGNALS)
    b_score, b_reasons = _apply_patterns(full_text, _BULK_SIGNALS)
    t_score, t_reasons = _apply_patterns(full_text, _TEMPORAL_SIGNALS)

    score += h_score + m_score + b_score + t_score
    reasons.extend(h_reasons + m_reasons + b_reasons + t_reasons)

    # ── 5. Short-circuit: obvious bulk / promotional mail ────────────────────
    if score <= _BULK_CUTOFF:
        return _finalize(
            "LOW", 
            score, 
            reasons or ["Bulk / automated / promotional mail detected"]
        )

    # ── 6. Short-circuit: very strong executive signals ──────────────────────
    if score >= _SCORE_HIGH:
        return _finalize("HIGH", score, reasons)

    # ── 7. AI classification for ambiguous emails ────────────────────────────
    try:
        print("🚀 AI BLOCK — score so far:", score)
        s_subj, s_content = _summary_source(email)
        # Use extended content for classification too!
        ai_result = classify_email_ai(s_subj, s_content, sender_domain)
        print("🔥 AI RESULT:", ai_result)

        if ai_result:
            ai_level      = ai_result["level"]
            ai_confidence = float(ai_result.get("confidence", 0.6))
            ai_reason     = ai_result.get("reason", "AI classification")
            ai_action     = ai_result.get("action") or "FYI"

            # Enforce minimum confidence for HIGH
            if ai_level == "HIGH" and ai_confidence < _HIGH_CONFIDENCE_FLOOR:
                ai_level = "MEDIUM"
                ai_reason = f"Downgraded from HIGH (low AI confidence {ai_confidence:.2f}): {ai_reason}"

            # If AI says LOW but keyword signals are clearly positive, trust keywords
            if ai_level == "LOW" and score >= _SCORE_MEDIUM:
                ai_level  = "MEDIUM"
                ai_reason = f"Keyword signals outweigh AI LOW assessment: {ai_reason}"

            # Summary will be handled by _finalize if we don't pass it here,
            # but we can try to get it now to keep the flow.
            summary = None
            try:
                summary = summarize_email(s_subj, s_content)
            except Exception:
                pass

            return _finalize(
                ai_level,
                int(ai_confidence * 100),
                reasons + [f"AI: {ai_reason}"],
                conf=ai_confidence,
                summ=summary,
                act=ai_action
            )

    except Exception as e:
        print("❌ AI FAILURE:", e)

    # ── 8. Keyword-only fallback (AI unavailable) ────────────────────────────
    if not reasons:
        reasons.append("No specific priority signals detected")

    if score >= _SCORE_HIGH:
        level = "HIGH"
    elif score >= _SCORE_MEDIUM:
        level = "MEDIUM"
    else:
        level = "LOW"

    return _finalize(level, score, reasons)

