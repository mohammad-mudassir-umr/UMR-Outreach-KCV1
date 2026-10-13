"""
Outreach Agent — templates.py

Template matching is deterministic (no AI). The proof point, offer, and CTA
in every email are your real proven copy from the 16-template playbook,
substituted verbatim — never AI-generated, so quality can't drift.

The opener (the one sentence describing something specific about the lead)
is built by a grammar-safe deterministic construction first, then AI is
given one shot to upgrade it to more natural prose — but only if its output
passes strict validation. This is deliberately NOT "ask AI to write a good
sentence and hope" — that approach produced repeated welded-clause errors
even after several rounds of prompt fixes, because a slot-filler forcing
research details into a fixed sentence stem can't grammatically accommodate
every shape a real detail takes (noun phrase, verb phrase, a job title, an
accidentally-pasted full sentence). The em-dash construction sidesteps that
entirely: "{{company}} — {{detail}}." parses correctly no matter what the
detail's own grammar looks like, so it's mechanically safe rather than
safe-if-the-model-behaves.
"""
import json
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Tuple

from leads import Lead


def load_template_library(path: Path) -> Dict[str, dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.pop("_schema_note", None)
    return data


def match_template(niche: str, library: Dict[str, dict]) -> Tuple[str, dict]:
    """Keyword match against the niche string. Falls back to the first
    template rather than crashing if nothing matches — better to send a
    slightly-off-niche email than skip the lead entirely."""
    niche_low = (niche or "").strip().lower()
    for tid, tmpl in library.items():
        if any(kw in niche_low for kw in tmpl.get("keywords", [])):
            return tid, tmpl
    first_id = next(iter(library))
    return first_id, library[first_id]


# ── Deterministic placeholder substitution (no AI, applied to every send) ──

def _substitute(text: str, lead: Lead, tmpl: dict, opener: str, sender_name: str) -> str:
    first_name = (lead.owner_name or "there").split()[0]  # first name only —
    # "Hi Bryan Messick," reads as a scraped list; "Hi Bryan," reads like a note
    out = (text
        .replace("{{first_name}}", first_name)
        .replace("{{company}}", lead.name)
        .replace("{{location}}", lead.location or "your area")
        .replace("{{opener}}", opener)
        .replace("{{sender_name}}", sender_name))

    # Solo-vs-team stat/proof selection (currently used by real_estate).
    # Templates that don't define these keys are unaffected — the
    # placeholders simply won't appear in their skeleton.
    if "{{stat_block}}" in out or "{{proof_block}}" in out:
        is_solo = (lead.business_type or "").strip().lower() == "solo"
        stat = tmpl.get("solo_stat_block") if is_solo else tmpl.get("team_stat_block")
        proof = tmpl.get("solo_proof_block") if is_solo else tmpl.get("team_proof_block")
        out = out.replace("{{stat_block}}", stat or "").replace("{{proof_block}}", proof or "")

    return out


# ── The opener: grammar-safe by construction, AI as an optional upgrade ────

def _build_deterministic_opener(lead: Lead, tmpl: dict) -> str:
    """The guaranteed-safe baseline. Works for ANY shape of raw detail —
    noun phrase, verb phrase, job title, even an accidentally-pasted full
    sentence — because the em-dash doesn't require grammatical agreement
    the way a fixed sentence stem does."""
    raw = (lead.trigger or lead.pain_point or tmpl["detail_fallback"]).strip().rstrip(".!").strip()
    return f"{lead.name} — {raw}."


def build_opener_prompt(lead: Lead, tmpl: dict) -> str:
    raw = (lead.trigger or lead.pain_point or tmpl["detail_fallback"]).strip()
    return f"""Write ONE complete, natural sentence for a cold email opener about {lead.name}, a
{lead.niche} business, describing this researched detail: "{raw}"

Real example of the natural-sentence style to match: "{tmpl['opener_example']}"

Rules:
- Exactly one sentence, ending in exactly one period.
- Use ONLY facts present in the detail above — never invent numbers, dates, or specifics not there.
- Pick whatever verb actually fits the detail grammatically (has/is/oversees/manages/is hiring/
  etc.) — do not force it into a fixed phrase.
- Must read as something a real person would say out loud, not a fragment or a mail-merge.
- If the detail is itself a full sentence or a job title, rewrite it as flowing prose describing
  the person or company — never paste it in unchanged.

Reply with ONLY this JSON, nothing else: {{"opener": "..."}}"""


def _validate_opener(text: str) -> bool:
    """Only accept an AI opener if it's unambiguously a single, complete,
    reasonably-sized sentence. Anything ambiguous falls back to the
    deterministic version rather than risking a welded clause."""
    text = text.strip()
    if not text or "{{" in text:
        return False
    if text.count(".") != 1 or not text.endswith("."):
        return False
    word_count = len(text.split())
    return 4 <= word_count <= 32


def get_opener(lead: Lead, tmpl: dict, providers=None) -> str:
    fallback = _build_deterministic_opener(lead, tmpl)
    if providers is None:
        return fallback
    try:
        raw_json = providers.generate_json(build_opener_prompt(lead, tmpl))
        candidate = str(raw_json.get("opener", "")).strip()
        if _validate_opener(candidate):
            return candidate
    except Exception:
        pass
    return fallback


# ── Subject line: picked, not generated — your proven options only ─────────

MAX_SUBJECT_CHARS = 40  # mobile truncation threshold — subjects longer than
                         # this reliably get cut off mid-word on a phone


def pick_subject(lead: Lead, tmpl: dict) -> str:
    """Stable per-lead pick across separate runs — NOT Python's built-in
    hash(), which is randomized per-process (PYTHONHASHSEED) and would
    silently pick a different subject line for the same lead every restart.

    Subject lines are all-lowercase, including the company name — mixed-case
    reads as a mail-merge artifact; full lowercase reads like a real note.

    Only chooses among options that actually fit under MAX_SUBJECT_CHARS
    once the real company name is substituted in — a long business name
    can blow a company-anchored option well past mobile truncation length.
    Each template carries 4 options specifically so that, even after
    length-filtering knocks one or two out for a long company name, there's
    still more than one real option left to rotate between — a single
    surviving option would mean every lead in that industry gets an
    identical subject line, which is its own failure pattern."""
    options = tmpl["subject_options"]
    rendered = [
        opt.replace("{{company}}", lead.name.lower())
           .replace("{{location}}", (lead.location or "your area").lower())
        for opt in options
    ]
    pool = [r for r in rendered if len(r) <= MAX_SUBJECT_CHARS] or [min(rendered, key=len)]
    digest = hashlib.md5(lead.email.encode("utf-8")).hexdigest()
    idx = int(digest, 16) % len(pool)
    return pool[idx]


def assemble_email(lead: Lead, tmpl: dict, opener: str, sender_name: str, brand_name: str) -> str:
    body = _substitute(tmpl["skeleton"], lead, tmpl, opener, sender_name)
    return f"{body}\n{brand_name}"


def deterministic_fallback(lead: Lead, tmpl: dict, sender_name: str, brand_name: str) -> Tuple[str, str]:
    """Zero-AI path — used when providers are unavailable/fail. Because the
    skeleton IS the proven copy (not AI-generated even in the normal path)
    and the opener construction is grammar-safe by design rather than by
    AI discipline, this produces output essentially identical in structural
    correctness to a successful AI run — the only difference is the opener
    uses the deterministic em-dash form instead of AI-polished prose."""
    opener = _build_deterministic_opener(lead, tmpl)
    subject = pick_subject(lead, tmpl)
    body = assemble_email(lead, tmpl, opener, sender_name, brand_name)
    return subject, body
