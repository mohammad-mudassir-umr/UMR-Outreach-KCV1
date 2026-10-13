"""
Outreach Agent — verification.py
MX-record check is free and unlimited — it's the default, always-on gate.
Verifalia (or similar) is optional and capped at a small daily budget, so it's
spent selectively rather than gating every single send.
"""
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import requests

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    DNS_AVAILABLE = False


def has_mx_record(email: str) -> bool:
    """Free, unlimited, no signup. Catches the majority of bad/fake domains."""
    if not DNS_AVAILABLE:
        return True   # fail open rather than block sends if dnspython isn't installed
    try:
        domain = email.split("@", 1)[1]
        dns.resolver.resolve(domain, "MX")
        return True
    except Exception:
        return False


class VerifaliaGate:
    """
    Optional, capped stricter check. Tracks its own daily usage in state/
    so it never silently exceeds the free-tier budget. Skips (returns None =
    "no opinion") once the daily cap is hit, rather than blocking sends.
    """
    def __init__(self, api_key: str, daily_cap: int, state_dir: Path):
        self.api_key   = api_key
        self.daily_cap = daily_cap
        self.usage_file = Path(state_dir) / "verifalia_usage.json"

    def _usage_today(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        if not self.usage_file.exists():
            return 0
        try:
            data = json.loads(self.usage_file.read_text(encoding="utf-8"))
        except Exception:
            return 0
        return data.get("count", 0) if data.get("date") == today else 0

    def _bump_usage(self):
        today = datetime.now().strftime("%Y-%m-%d")
        count = self._usage_today() + 1
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.usage_file.parent), prefix="verifalia.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"date": today, "count": count}, f)
            shutil.move(tmp_name, self.usage_file)
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)

    def check(self, email: str) -> "bool | None":
        if not self.api_key:
            return None
        if self._usage_today() >= self.daily_cap:
            logging.info("Verifalia daily cap reached — skipping (MX check already passed).")
            return None
        # The integration is intentionally disabled until the submit/poll lifecycle
        # is fully implemented and tested against a live account. A half-integrated
        # validator is worse than no validator because it can consume quota without
        # changing the send decision. MX verification remains the active gate.
        logging.warning("Verifalia API key is configured, but Verifalia validation is not implemented; skipping it.")
        return None


def verify_email(email: str, verifalia: "VerifaliaGate | None" = None) -> bool:
    if not has_mx_record(email):
        return False
    if verifalia:
        result = verifalia.check(email)
        if result is False:
            return False
    return True
