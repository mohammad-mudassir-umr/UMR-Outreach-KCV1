"""
Outreach Agent — leads.py
Lead ingestion. LeadSource is an interface so swapping CSV for a live
Google Sheet later is a one-file change, not a rewrite.

Minimum required columns:  Name, Email, Niche
Recommended optional:       Location, Pain Point, Website, Offer/Notes, Trigger
"""
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Lead:
    name: str
    email: str
    niche: str
    owner_name: str  = ""    # first name used in "Hi {name}," — falls back to "there" if blank
    business_type: str = ""  # "solo" or "team" — currently used by real_estate to pick a
                              # stat/proof point that actually matches the prospect; blank
                              # defaults to "team" (the original proven copy)
    location: str    = ""
    pain_point: str  = ""
    website: str     = ""
    trigger: str     = ""
    notes: str        = ""
    template_id: str  = ""   # filled in by templates.py once matched
    subject_variant: str = ""


class LeadSource:
    """Interface. Implement load() to return a deduped, validated list of Leads."""
    def load(self) -> List[Lead]:
        raise NotImplementedError


class CSVLeadSource(LeadSource):
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> List[Lead]:
        if not self.path.exists():
            raise FileNotFoundError(f"Leads file not found: {self.path}")

        with self.path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            # Normalize header names so "email", "Email", " Email " all work.
            field_map = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}

            def col(row, *names):
                for n in names:
                    key = field_map.get(n)
                    if key and row.get(key):
                        return str(row[key]).strip()
                return ""

            leads: List[Lead] = []
            for row in reader:
                email = col(row, "email").lower()
                name  = col(row, "name", "lead_name", "business name", "company")
                niche = col(row, "niche", "industry")
                if not email or not niche:
                    continue   # required columns missing — skip, don't crash the batch
                leads.append(Lead(
                    name=name or "there",
                    email=email,
                    niche=niche,
                    owner_name=col(row, "owner name", "owner", "contact name", "first name") or "there",
                    business_type=col(row, "business type", "team size", "solo or team"),
                    location=col(row, "location", "city"),
                    pain_point=col(row, "pain point", "pain_point", "pain"),
                    website=col(row, "website"),
                    trigger=col(row, "trigger", "trigger event", "trigger_event"),
                    notes=col(row, "notes", "offer", "offer/notes"),
                ))
        return leads


# ── Google Sheets — stub for later ──────────────────────────────────────────
# class SheetsLeadSource(LeadSource):
#     def __init__(self, sheet_id: str, worksheet: str = "Leads"):
#         ...
#     def load(self) -> List[Lead]:
#         # pull rows via the Sheets API, map to the same Lead fields as above
#         raise NotImplementedError("Wire up google-api-python-client + a service account here")


def dedupe_against(leads: List[Lead], already_emailed: set, blacklisted: set) -> List[Lead]:
    out = []
    for lead in leads:
        if lead.email in already_emailed or lead.email in blacklisted:
            continue
        out.append(lead)
    return out
