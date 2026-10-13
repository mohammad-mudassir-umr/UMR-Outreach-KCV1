"""
Outreach Agent — emailer.py
SMTP + app password (not the OAuth Gmail API — no app-verification hassle).
Warmup ramps daily volume up rather than jumping straight to a flat cap.
"""
import csv
import json
import logging
import os
import shutil
import smtplib
import tempfile
import threading
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import List

_FILE_LOCKS = {}
_LOCKS_GUARD = threading.Lock()

def _file_lock(path: Path):
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.Lock())


SENT_LOG_HEADER = [
    "Name", "Email", "Sent From", "Niche", "Location", "Template", "Subject Variant",
    "Date Sent", "Follow-up Sent", "Reply Status",
]


class SentLog:
    """Atomic read-modify-write, same pattern as KCv1's leads.csv handling."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._write([SENT_LOG_HEADER])

    def _read(self) -> List[List[str]]:
        with self.path.open("r", encoding="utf-8", newline="") as f:
            return list(csv.reader(f))

    def _write(self, rows: List[List]):
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
            shutil.move(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def already_sent(self) -> set:
        rows = self._read()
        return {(r[1] or "").strip().lower() for r in rows[1:] if len(r) > 1}

    def record_send(self, name, email, sent_from, niche, location, template_id, subject_variant):
        with _file_lock(self.path):
            rows = self._read()
            rows.append([name, email, sent_from, niche, location, template_id, subject_variant,
                         datetime.now().strftime("%Y-%m-%d"), "False", "None"])
            self._write(rows)


class Blacklist:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> set:
        if not self.path.exists():
            return set()
        return {l.strip().lower() for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()}

    def add(self, email: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.path):
            values = self.load()
            values.add(email.strip().lower())
            self.path.write_text("\n".join(sorted(values)) + "\n", encoding="utf-8")


class Warmup:
    """Per-account daily limits and optional per-account warm-up progression.

    account_settings is intentionally supplied by the caller so the existing
    mailer remains independent of dashboard storage.
    """
    def __init__(self, path: Path, accounts: List[str], start_limit: int = 5,
                 increment: int = 0, cap: int = 100, account_settings=None):
        self.path = Path(path)
        self.accounts = accounts
        self.start_limit = max(1, int(start_limit))
        self.increment = max(0, int(increment))
        self.cap = max(self.start_limit, int(cap))
        self.account_settings = account_settings or {}

    def _configured(self, acct):
        s = self.account_settings.get(acct, {}) or {}
        try:
            limit = max(1, int(s.get("daily_limit", self.start_limit)))
        except (TypeError, ValueError):
            limit = self.start_limit
        try:
            inc = max(0, int(s.get("warmup_increment", self.increment)))
        except (TypeError, ValueError):
            inc = self.increment
        try:
            cap = max(limit, int(s.get("warmup_max", self.cap)))
        except (TypeError, ValueError):
            cap = max(limit, self.cap)
        return limit, bool(s.get("warmup_enabled", False)), inc, cap

    def load(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        existing = {}
        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        prev_accounts = existing.get("accounts", {})
        same_day = existing.get("date") == today
        accounts_state = {}
        for acct in self.accounts:
            prev = prev_accounts.get(acct, {})
            configured_limit, enabled, increment, cap = self._configured(acct)
            if same_day:
                limit = configured_limit if acct in self.account_settings else prev.get("daily_limit", configured_limit)
                accounts_state[acct] = {
                    "sent_today": max(0, int(prev.get("sent_today", 0))),
                    "daily_limit": max(1, min(int(limit), cap)),
                }
            else:
                previous_limit = int(prev.get("daily_limit", configured_limit)) if prev else configured_limit
                next_limit = min(previous_limit + increment, cap) if prev and enabled else configured_limit
                accounts_state[acct] = {
                    "sent_today": 0,
                    "daily_limit": max(1, min(next_limit, cap)),
                }
        return {"date": today, "accounts": accounts_state}

    def save(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix="warmup.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            shutil.move(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def remaining(self, data: dict, account: str) -> int:
        a = data["accounts"].get(account, {"sent_today": 0, "daily_limit": self.start_limit})
        return max(0, int(a.get("daily_limit", self.start_limit)) - int(a.get("sent_today", 0)))

    def record_sent(self, data: dict, account: str):
        data["accounts"].setdefault(account, {"sent_today": 0, "daily_limit": self.start_limit})
        data["accounts"][account]["sent_today"] += 1


class Bounces:
    """Simple counter for the dashboard's bounce rate — incremented whenever
    SMTP actually refuses a recipient, not estimated or fabricated."""
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {"bounced": 0, "attempted": 0}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"bounced": 0, "attempted": 0}

    def record_attempt(self, bounced: bool):
        with _file_lock(self.path):
            data = self.load()
            data["attempted"] = data.get("attempted", 0) + 1
            if bounced:
                data["bounced"] = data.get("bounced", 0) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix="bounces.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                shutil.move(tmp_name, self.path)
            finally:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass


def send_email(cfg, to_email: str, subject: str, body: str, account: str):
    """Returns the sending account's email on success, None on failure —
    callers use this both as a truthy success check and to know exactly
    which account sent it, for logging and the dashboard's per-account view."""
    accounts_by_email = {u: p for u, p in cfg.gmail_accounts}
    persona = cfg.gmail_personas_by_email.get(account, cfg.identity_name)

    if cfg.dry_run:
        logging.info("[DRY RUN] %s as '%s' -> %s | %s\n%s", account, persona, to_email, subject, body)
        return account

    pwd = accounts_by_email[account]
    bounces = Bounces(cfg.state_dir / "bounces.json")
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"]     = f"{persona} | {cfg.brand_name} <{account}>"
        msg["To"]       = to_email
        msg["Subject"]  = subject
        msg["Reply-To"] = cfg.forward_email
        # Gmail's 2024+ bulk-sender rules increasingly expect this even
        # below the 5,000/day threshold. Using mailto (not the RFC 8058
        # HTTPS one-click form) since there's no hosted unsubscribe page —
        # List-Unsubscribe-Post is deliberately omitted, since that header
        # specifically signals the HTTPS POST variant and would be
        # misleading paired with a mailto-only address.
        msg["List-Unsubscribe"] = f"<mailto:{account}?subject=unsubscribe>"
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(account, pwd)
            server.send_message(msg)
        bounces.record_attempt(bounced=False)
        return account
    except smtplib.SMTPRecipientsRefused as e:
        logging.error("SMTP refused %s: %s", to_email, e)
        Blacklist(cfg.blacklist_file).add(to_email)
        bounces.record_attempt(bounced=True)
        return None
    except Exception as e:
        logging.error("send_email failed -> %s via %s: %s", to_email, account, e)
        bounces.record_attempt(bounced=False)
        return None
