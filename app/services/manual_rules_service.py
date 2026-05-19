import json
import os
from threading import Lock

RULES_FILE = "app/manual_priority_rules.json"
_file_lock = Lock()


def _ensure_file():
    if not os.path.exists(RULES_FILE):
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "high_priority_senders": [],
                "rules": []  # ✅ NEW
            }, f, indent=2)


def load_rules():
    _ensure_file()
    with _file_lock:
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def save_rules(data):
    with _file_lock:
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


# ================= EXISTING =================

def get_high_priority_senders():
    data = load_rules()
    return data.get("high_priority_senders", [])


def add_high_priority_sender(sender_email: str):
    sender_email = (sender_email or "").strip().lower()
    if not sender_email:
        return False

    data = load_rules()
    senders = data.get("high_priority_senders", [])

    if sender_email not in senders:
        senders.append(sender_email)
        data["high_priority_senders"] = sorted(senders)
        save_rules(data)
        return True

    return False


def remove_high_priority_sender(sender_email: str):
    sender_email = (sender_email or "").strip().lower()
    data = load_rules()
    senders = data.get("high_priority_senders", [])

    if sender_email in senders:
        senders.remove(sender_email)
        data["high_priority_senders"] = sorted(senders)
        save_rules(data)
        return True

    return False


# ================= NEW RULE ENGINE =================

def get_rules():
    data = load_rules()
    return data.get("rules", [])


def add_rule(rule: dict):
    """
    rule format:
    {
        "field": "subject",
        "operator": "contains",
        "value": "invoice",
        "priority": "HIGH"
    }
    """
    data = load_rules()
    rules = data.get("rules", [])

    # avoid duplicates
    if rule not in rules:
        rules.append(rule)
        data["rules"] = rules
        save_rules(data)
        return True

    return False


def remove_rule(rule: dict):
    data = load_rules()
    rules = data.get("rules", [])

    if rule in rules:
        rules.remove(rule)
        data["rules"] = rules
        save_rules(data)
        return True

    return False


def apply_rules(email: dict):
    """
    Apply manual rules BEFORE keyword logic
    """
    rules = get_rules()

    subject = (email.get("subject") or "").lower()

    for rule in rules:
        field = rule.get("field")
        operator = rule.get("operator")
        value = (rule.get("value") or "").lower()
        priority = rule.get("priority")

        if field == "subject":
            if operator == "contains" and value in subject:
                return {
                    "level": priority,
                    "score": 90,
                    "reasons": [f'Rule match: subject contains "{value}"'],
                }

    return None