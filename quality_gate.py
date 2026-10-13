"""
Outreach Agent — quality_gate.py

Deterministic checks in place of a heavyweight eval platform (Braintrust/
Langfuse). Catches the failure modes that actually matter for a slot-filled
template: leaked placeholders, off-brand length, spam-trigger language,
missing compliance footer. If anything fails, the caller should use
templates.deterministic_fallback() rather than retry-and-hope.
"""
import re
from typing import List, Tuple

SPAM_TRIGGER_WORDS = {
    "free", "guarantee", "guaranteed", "act now", "limited time offer",
    "click here", "buy now", "no obligation", "risk-free", "winner",
    "congratulations", "urgent", "cash bonus",
}

UNSUBSCRIBE_PHRASES = (
    "unsubscribe", "opt out", "opt-out", "no longer wish",
    "stop receiving", "remove me",
)


def append_compliance_footer(body: str) -> str:
    low = body.lower()
    if any(p in low for p in UNSUBSCRIBE_PHRASES):
        return body
    return body.rstrip() + '\n\nIf you\'d rather not hear from us, reply "unsubscribe" and we\'ll stop.'


def validate_email(subject: str, body: str, min_words: int = 30, max_words: int = 160) -> Tuple[bool, List[str]]:
    reasons = []

    if re.search(r"\{\{.*?\}\}", body) or re.search(r"\{\{.*?\}\}", subject):
        reasons.append("leaked {{placeholder}} — a slot failed to fill")

    word_count = len(body.split())
    if word_count < min_words:
        reasons.append(f"body too short ({word_count} words)")
    if word_count > max_words:
        reasons.append(f"body too long ({word_count} words)")

    low_body = body.lower()
    low_subj = subject.lower()
    hit_words = [w for w in SPAM_TRIGGER_WORDS if w in low_body or w in low_subj]
    if hit_words:
        reasons.append(f"spam-trigger language: {', '.join(hit_words)}")

    if not any(p in low_body for p in UNSUBSCRIBE_PHRASES):
        reasons.append("missing compliance/unsubscribe footer")

    if len(subject.strip()) < 5:
        reasons.append("subject line missing or too short")

    caps_ratio = sum(1 for c in subject if c.isupper()) / max(len(subject), 1)
    if caps_ratio > 0.4:
        reasons.append("subject line excessive caps (looks spammy)")

    return (len(reasons) == 0, reasons)
