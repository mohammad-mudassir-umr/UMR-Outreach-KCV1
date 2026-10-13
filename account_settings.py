"""
Per-account runtime routing settings.

Credentials/display names live in accounts.json. This file stores only the
non-secret controls that can change independently: active/paused state and
niche routing.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / "account_settings.json"


def _normalize(item: dict) -> dict:
    niches = item.get("niches", [])
    if not isinstance(niches, list):
        niches = []
    try:
        daily_limit = max(1, int(item.get("daily_limit", 5)))
    except (TypeError, ValueError):
        daily_limit = 5
    try:
        increment = max(0, int(item.get("warmup_increment", 0)))
    except (TypeError, ValueError):
        increment = 0
    try:
        warmup_max = max(daily_limit, int(item.get("warmup_max", daily_limit)))
    except (TypeError, ValueError):
        warmup_max = daily_limit
    return {
        "active": bool(item.get("active", True)),
        "niches": [str(n).strip() for n in niches if str(n).strip()],
        "daily_limit": daily_limit,
        "warmup_enabled": bool(item.get("warmup_enabled", False)),
        "warmup_increment": increment,
        "warmup_max": warmup_max,
    }


def load(state_dir: Path) -> Dict[str, dict]:
    p = _path(state_dir)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(email).strip().lower(): _normalize(item if isinstance(item, dict) else {})
            for email, item in raw.items() if str(email).strip()}


def save(state_dir: Path, data: Dict[str, dict]):
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {str(email).strip().lower(): _normalize(item if isinstance(item, dict) else {})
             for email, item in data.items() if str(email).strip()}
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix="account_settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
            f.write("\n")
        shutil.move(tmp_name, p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def get(state_dir: Path, email: str) -> dict:
    return load(state_dir).get(str(email).strip().lower(), {"active": True, "niches": [], "daily_limit": 5, "warmup_enabled": False, "warmup_increment": 0, "warmup_max": 5})


def is_eligible(state_dir: Path, email: str, template_id: str, settings: Dict[str, dict] = None) -> bool:
    data = settings if settings is not None else load(state_dir)
    s = data.get(str(email).strip().lower(), {"active": True, "niches": [], "daily_limit": 5, "warmup_enabled": False, "warmup_increment": 0, "warmup_max": 5})
    if not s.get("active", True):
        return False
    niches = s.get("niches", [])
    return not niches or template_id in niches
