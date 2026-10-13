"""
Local Gmail account store for the outreach agent.

Each account contains only the data required by the SMTP sender:
- name: display/from name
- email: Gmail address
- app_password: Gmail app password

The dashboard never returns app passwords to the browser. CRUD operations are
blocked while the agent is running so the running worker and dashboard cannot
race over the account set.
"""
from __future__ import annotations

import json
import os
import re
import uuid
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

ACCOUNT_FILE_NAME = "accounts.json"
_EMAIL_RE = re.compile(r"^[^@\s]+@gmail\.com$", re.IGNORECASE)


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / ACCOUNT_FILE_NAME


def _normalize(record: dict) -> dict:
    return {
        "id": str(record.get("id") or uuid.uuid4()),
        "name": str(record.get("name") or "").strip(),
        "email": str(record.get("email") or "").strip().lower(),
        "app_password": str(record.get("app_password") or "").replace(" ", "").strip(),
    }


def _write(path: Path, data: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="accounts.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        shutil.move(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def load(state_dir: Path) -> List[dict]:
    path = _path(state_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    accounts = []
    for item in raw:
        if isinstance(item, dict):
            accounts.append(_normalize(item))
    return accounts


def public(account: dict) -> dict:
    return {
        "id": account["id"],
        "name": account["name"],
        "email": account["email"],
    }


def public_all(state_dir: Path) -> List[dict]:
    return [public(a) for a in load(state_dir)]


def list_accounts(state_dir: Path) -> List[dict]:
    return load(state_dir)


def get(state_dir: Path, account_id: str) -> Optional[dict]:
    return next((a for a in load(state_dir) if a["id"] == account_id), None)


def add(state_dir: Path, name: str, email: str, app_password: str) -> dict:
    name = str(name or "").strip()
    email = str(email or "").strip().lower()
    app_password = str(app_password or "").replace(" ", "").strip()
    if not name:
        raise ValueError("Account name is required.")
    if not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid @gmail.com address.")
    if not app_password:
        raise ValueError("App password is required.")

    accounts = load(state_dir)
    if any(a["email"].casefold() == email.casefold() for a in accounts):
        raise ValueError("That Gmail account is already added.")

    account = _normalize({"name": name, "email": email, "app_password": app_password})
    accounts.append(account)
    _write(_path(state_dir), accounts)
    return account


def remove(state_dir: Path, account_id: str) -> bool:
    accounts = load(state_dir)
    filtered = [a for a in accounts if a["id"] != account_id]
    if len(filtered) == len(accounts):
        return False
    _write(_path(state_dir), filtered)
    return True
