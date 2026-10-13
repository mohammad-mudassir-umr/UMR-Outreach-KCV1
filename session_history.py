"""Persistent agent session timing/history helpers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)

def parse_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        # Sent-log timestamps historically have no offset; interpret them in
        # the machine's local timezone so they align with dashboard sessions.
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt

def session_sent_count(sent_rows, start_iso, end_iso):
    start = parse_datetime(start_iso)
    end = parse_datetime(end_iso)
    if not start or not end:
        return 0
    return sum(1 for row in sent_rows if (lambda x: x is not None and start <= x <= end)(parse_datetime(row.get("Date Sent"))))

def load_history(path: Path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def finalize_active_session(session_path: Path, history_path: Path, sent_rows, end_dt=None, reason="stopped"):
    if not session_path.exists():
        return None
    try:
        active = json.loads(session_path.read_text(encoding="utf-8"))
    except Exception:
        active = None
    if not isinstance(active, dict) or not active.get("started_at"):
        session_path.unlink(missing_ok=True)
        return None
    end_dt = end_dt or datetime.now(timezone.utc)
    end_iso = end_dt.isoformat()
    start_dt = parse_datetime(active["started_at"]) or end_dt
    duration = max(0.0, (end_dt - start_dt).total_seconds())
    session = {
        "id": active.get("id", int(start_dt.timestamp() * 1000)),
        "started_at": active["started_at"],
        "ended_at": end_iso,
        "duration_seconds": round(duration, 3),
        "emails_sent": session_sent_count(sent_rows, active["started_at"], end_iso),
        "emails_failed": max(0, int(active.get("emails_failed", 0))),
        "reason": reason,
    }
    history = load_history(history_path)
    history.append(session)
    atomic_write_json(history_path, history[-100:])
    session_path.unlink(missing_ok=True)
    return session
