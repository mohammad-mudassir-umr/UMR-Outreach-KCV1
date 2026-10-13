"""
Outreach Agent — dashboard.py
Flask dashboard: upload leads, per-account routing/limits, structured
multi-account view, Settings (AI keys, limits), live logs.
"""
import csv
import json
import os
import signal
import subprocess
import sys
import time
import smtplib
import threading
from email.message import EmailMessage
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
import account_settings
import accounts as gmail_accounts_store
from config import Config
from emailer import Warmup
from observability import capture_exception, capture_message, init_sentry

app = Flask(__name__)
init_sentry()
START_LOCK = threading.Lock()

BASE_DIR      = Path(__file__).resolve().parent
STATE_DIR     = BASE_DIR / "state"
DATA_DIR      = BASE_DIR / "data"
LEADS_CSV     = DATA_DIR / "leads.csv"
SENT_LOG_CSV  = DATA_DIR / "sent_log.csv"
WARMUP_FILE   = STATE_DIR / "warmup_state.json"
BOUNCES_FILE  = STATE_DIR / "bounces.json"
HEALTH_FILE   = STATE_DIR / "provider_health.json"
LOG_FILE      = STATE_DIR / "agent.log"
PID_FILE      = STATE_DIR / "agent.pid"
SESSION_FILE  = STATE_DIR / "active_session.json"
SESSION_HISTORY_FILE = STATE_DIR / "session_history.json"
ENV_FILE      = BASE_DIR / ".env"

STATE_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

NICHES = [
    ("auto_repair", "Auto Repair"), ("real_estate", "Real Estate"),
    ("restaurants", "Restaurants"), ("med_spas", "Med Spas"),
    ("gyms", "Gyms"), ("ecommerce", "E-commerce"),
    ("financial_advisors", "Financial Advisors"), ("marketing_agencies", "Marketing Agencies"),
]

# ── Settings (.env) read/write ───────────────────────────────────────────
SETTINGS_FIELDS = [
    {"key": "DRY_RUN",            "label": "Dry Run (no real sends)", "type": "bool"},
    {"key": "IDENTITY_NAME",      "label": "Sender name",             "type": "text"},
    {"key": "BRAND_NAME",         "label": "Brand name",              "type": "text"},
    {"key": "FORWARD_EMAIL",      "label": "Reply-to address",        "type": "text"},
    {"key": "GLOBAL_DAILY_MAX",   "label": "Global maximum daily limit", "type": "number"},
    {"key": "SENDING_STRATEGY",   "label": "Sending strategy",         "type": "select", "options": ["auto", "balanced", "capacity_first", "round_robin"]},
    {"key": "AI_WORKER_COUNT",    "label": "AI generation workers",    "type": "number"},
    {"key": "SEND_WORKER_COUNT",  "label": "Send workers",             "type": "number"},
    {"key": "PIPELINE_QUEUE_SIZE","label": "Queue size",               "type": "number"},
]
MULTI_KEY_FIELDS = [
    {"key": "GROQ_API_KEYS",       "label": "Groq"},
    {"key": "AGNES_API_KEYS",      "label": "Agnes AI"},
    {"key": "POIXE_API_KEYS",      "label": "Poixe AI"},
    {"key": "OPENROUTER_API_KEYS", "label": "OpenRouter"},
]


def _read_env_lines():
    return ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []


def _parse_env(lines):
    values = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        values[k.strip()] = v.strip()
    return values


def apply_settings_update(updates: dict):
    lines = _read_env_lines()
    seen, new_lines = set(), []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, _ = s.partition("=")
            k = k.strip()
            if k in updates and updates[k] != "":
                new_lines.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in seen and v != "":
            new_lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def get_settings_for_ui():
    values = _parse_env(_read_env_lines())
    out = [{**f, "value": values.get(f["key"], "")} for f in SETTINGS_FIELDS]
    multi = []
    for f in MULTI_KEY_FIELDS:
        count = len([k for k in values.get(f["key"], "").split(",") if k.strip()])
        multi.append({**f, "count": count})
    return out, multi


@app.route("/api/settings", methods=["GET"])
def get_settings():
    fields, multi = get_settings_for_ui()
    return jsonify({"fields": fields, "multi_key_fields": multi})


@app.route("/api/settings", methods=["POST"])
def save_settings():
    if agent_running():
        return jsonify({"ok": False, "msg": "Stop the agent before changing settings."})
    body = request.get_json(force=True, silent=True) or {}
    values = _parse_env(_read_env_lines())
    updates = {}

    for f in SETTINGS_FIELDS:
        if f["key"] not in body:
            continue
        val = body[f["key"]]
        if f["type"] == "bool":
            updates[f["key"]] = "true" if bool(val) else "false"
        elif f["type"] == "select":
            value = str(val).strip().lower()
            if value not in f.get("options", []):
                return jsonify({"ok": False, "msg": f"{f['label']} has an invalid value."})
            updates[f["key"]] = value
        elif f["type"] == "number":
            try:
                number = int(str(val).strip())
            except (TypeError, ValueError):
                return jsonify({"ok": False, "msg": f"{f['label']} must be a whole number."})
            minimum = 1
            if f["key"] == "PIPELINE_QUEUE_SIZE":
                minimum = 10
            if number < minimum:
                return jsonify({"ok": False, "msg": f"{f['label']} must be at least {minimum}."})
            updates[f["key"]] = str(number)
        else:
            text = str(val).strip()
            if f["key"] == "FORWARD_EMAIL" and text and ("@" not in text or " " in text):
                return jsonify({"ok": False, "msg": "Reply-to address must look like an email address."})
            updates[f["key"]] = text

    # Multi-key fields: {"keep_indices": [0,2], "new_keys": ["sk-..."]}
    # — never round-trips an existing secret value, only which original
    # slots to keep (by index) plus any brand-new keys typed this time.
    for f in MULTI_KEY_FIELDS:
        if f["key"] not in body:
            continue
        spec = body[f["key"]]
        existing = [k.strip() for k in values.get(f["key"], "").split(",") if k.strip()]
        keep = [existing[i] for i in spec.get("keep_indices", []) if 0 <= i < len(existing)]
        new_keys = [k.strip() for k in spec.get("new_keys", []) if k.strip()]
        updates[f["key"]] = ",".join(keep + new_keys)

    apply_settings_update(updates)
    return jsonify({"ok": True})


# ── Gmail account manager ──────────────────────────────────────────────────

def get_configured_accounts():
    return gmail_accounts_store.public_all(STATE_DIR)

def _smtp_test(email: str, app_password: str):
    server = None
    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=12)
        server.login(email, app_password)
        return True, "Connection verified."
    except smtplib.SMTPAuthenticationError:
        return False, "Gmail rejected the address or app password. Check the credentials and try again."
    except (smtplib.SMTPException, OSError) as e:
        return False, f"Could not connect to Gmail: {e}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass

@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    return jsonify({"accounts": get_configured_accounts()})

@app.route("/api/accounts", methods=["POST"])
def add_account():
    if agent_running():
        return jsonify({"ok": False, "msg": "Stop the agent before changing Gmail accounts."})
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get("name") or "").strip()
    email = str(body.get("email") or "").strip().lower()
    app_password = str(body.get("app_password") or "").replace(" ", "").strip()
    if not name or not email or not app_password:
        return jsonify({"ok": False, "msg": "Account name, Gmail address, and app password are all required."})
    ok, message = _smtp_test(email, app_password)
    if not ok:
        return jsonify({"ok": False, "msg": message, "stage": "verification"})
    try:
        account = gmail_accounts_store.add(STATE_DIR, name, email, app_password)
        # Preserve the existing routing contract: new accounts are active and
        # unrestricted until the operator chooses otherwise.
        settings = account_settings.load(STATE_DIR)
        settings.setdefault(account["email"], {"active": True, "niches": []})
        account_settings.save(STATE_DIR, settings)
        return jsonify({"ok": True, "account": gmail_accounts_store.public(account)})
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def delete_account(account_id):
    if agent_running():
        return jsonify({"ok": False, "msg": "Stop the agent before changing Gmail accounts."})
    account = gmail_accounts_store.get(STATE_DIR, account_id)
    if not account:
        return jsonify({"ok": False, "msg": "Account not found."})
    if not gmail_accounts_store.remove(STATE_DIR, account_id):
        return jsonify({"ok": False, "msg": "Account could not be removed."})
    settings = account_settings.load(STATE_DIR)
    settings.pop(account["email"], None)
    account_settings.save(STATE_DIR, settings)
    return jsonify({"ok": True})

# ── Account routing + per-account sending controls ────────────────────────

def _ensure_account_controls():
    """One-time migration from the old global warm-up controls."""
    raw = {}
    if (STATE_DIR / "account_settings.json").exists():
        try:
            raw = json.loads((STATE_DIR / "account_settings.json").read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    legacy = read_json(WARMUP_FILE, {}).get("accounts", {})
    settings = account_settings.load(STATE_DIR)
    changed = False
    for a in get_configured_accounts():
        email = a["email"]
        raw_item = raw.get(email, {}) if isinstance(raw, dict) else {}
        if "daily_limit" not in raw_item:
            old = legacy.get(email, {})
            limit = max(1, int(old.get("daily_limit", 5)))
            settings[email] = {
                "active": settings.get(email, {}).get("active", True),
                "niches": settings.get(email, {}).get("niches", []),
                "daily_limit": limit,
                "warmup_enabled": True,
                "warmup_increment": 3,
                "warmup_max": max(limit, 100),
            }
            changed = True
    if changed:
        account_settings.save(STATE_DIR, settings)
    return settings

@app.route("/api/account_settings", methods=["GET"])
def get_account_settings():
    settings = _ensure_account_controls()
    out = {}
    for a in get_configured_accounts():
        out[a["email"]] = settings.get(a["email"], {
            "active": True, "niches": [], "daily_limit": 5,
            "warmup_enabled": False, "warmup_increment": 0, "warmup_max": 5
        })
    return jsonify({"settings": out, "niches": NICHES})


@app.route("/api/account_settings", methods=["POST"])
def save_account_settings():
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "msg": "email required"})
    configured = {a["email"] for a in get_configured_accounts()}
    if email not in configured:
        return jsonify({"ok": False, "msg": "That Gmail account is not configured."})
    niches = body.get("niches", [])
    if not isinstance(niches, list):
        return jsonify({"ok": False, "msg": "niches must be a list"})
    settings = account_settings.load(STATE_DIR)
    current = settings.get(email, {})
    try:
        daily_limit = max(1, int(body.get("daily_limit", current.get("daily_limit", 5))))
        warmup_increment = max(0, int(body.get("warmup_increment", current.get("warmup_increment", 0))))
        warmup_max = max(daily_limit, int(body.get("warmup_max", current.get("warmup_max", daily_limit))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "Account limits must be whole numbers."})
    if warmup_increment > warmup_max:
        return jsonify({"ok": False, "msg": "Warm-up increment cannot exceed the account maximum."})
    settings[email] = {
        "active": bool(body.get("active", current.get("active", True))),
        "niches": [str(n).strip() for n in niches if str(n).strip()],
        "daily_limit": daily_limit,
        "warmup_enabled": bool(body.get("warmup_enabled", current.get("warmup_enabled", False))),
        "warmup_increment": warmup_increment,
        "warmup_max": warmup_max,
    }
    account_settings.save(STATE_DIR, settings)
    return jsonify({"ok": True, "email": email, "active": settings[email]["active"],
                    "niches": settings[email]["niches"]})


def read_csv_dicts(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_log_tail(n=80):
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open("r", encoding="utf-8") as f:
        return [l.rstrip() for l in f.readlines()[-n:]]


from session_history import atomic_write_json, finalize_active_session, load_history, parse_datetime, session_sent_count


def _load_session_history():
    return load_history(SESSION_HISTORY_FILE)


def _finalize_active_session(end_dt=None, reason="stopped"):
    return finalize_active_session(SESSION_FILE, SESSION_HISTORY_FILE, read_csv_dicts(SENT_LOG_CSV), end_dt=end_dt, reason=reason)


def _reconcile_session():
    if SESSION_FILE.exists() and not agent_pid():
        return _finalize_active_session(reason="completed")
    return None


def agent_pid():
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        PID_FILE.unlink(missing_ok=True)
        return None


def agent_running():
    pid = agent_pid()
    if pid is None:
        _reconcile_session()
        return False
    return True


@app.route("/api/upload_leads", methods=["POST"])
def upload_leads():
    if "file" not in request.files:
        return jsonify({"ok": False, "msg": "No file uploaded"})
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "msg": "Empty filename"})
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"ok": False, "msg": "Only .csv is supported (export xlsx to CSV first)"})
    import io
    try:
        raw = f.stream.read()
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
        if not {"name", "email", "niche"}.issubset(headers):
            return jsonify({"ok": False, "msg": "CSV must contain Name, Email, and Niche columns."})
        rows = list(reader)
        if not rows:
            return jsonify({"ok": False, "msg": "CSV contains no lead rows."})
        invalid_emails = 0
        for row in rows:
            email = next((str(row.get(h) or "").strip() for h in row if h and h.strip().lower() == "email"), "")
            if email and ("@" not in email or email.startswith("@") or email.endswith("@")):
                invalid_emails += 1
        if invalid_emails == len(rows):
            return jsonify({"ok": False, "msg": "CSV has no usable email addresses."})
        LEADS_CSV.write_bytes(raw)
        return jsonify({"ok": True, "rows": len(rows), "invalid_email_rows": invalid_emails})
    except UnicodeDecodeError:
        return jsonify({"ok": False, "msg": "CSV must be UTF-8 encoded."})


def compute_account_stats(sent_rows, warmup_accounts, configured_accounts, default_start_limit):
    per_account = {}
    for r in sent_rows:
        acct = r.get("Sent From") or "unknown"
        a = per_account.setdefault(acct, {"total_sent": 0, "replies": 0})
        a["total_sent"] += 1
        if r.get("Reply Status") not in ("None", "", None):
            a["replies"] += 1

    configured_by_email = {c["email"]: c for c in configured_accounts}
    all_emails = set(per_account) | set(warmup_accounts) | set(configured_by_email)
    out = []
    for email in sorted(all_emails):
        a = per_account.get(email, {"total_sent": 0, "replies": 0})
        w = warmup_accounts.get(email, {"sent_today": 0, "daily_limit": default_start_limit})
        meta = configured_by_email.get(email, {"id": "", "name": email.split("@")[0]})
        out.append({
            "id": meta.get("id", ""),
            "name": meta.get("name", email.split("@")[0]),
            "email": email,
            "sent_today": w.get("sent_today", 0),
            "daily_limit": w.get("daily_limit", default_start_limit),
            "total_sent": a["total_sent"],
            "replies": a["replies"],
            "reply_rate": round(a["replies"] / a["total_sent"] * 100, 1) if a["total_sent"] else 0,
        })
    return out


@app.route("/api/session")
def session_status():
    _reconcile_session()
    active = read_json(SESSION_FILE, None)
    history = _load_session_history()
    if not active:
        return jsonify({"active": False, "session": None, "history": list(reversed(history))})
    now = datetime.now(timezone.utc)
    start = parse_datetime(active.get("started_at")) or now
    elapsed = max(0.0, (now - start).total_seconds())
    sent = session_sent_count(read_csv_dicts(SENT_LOG_CSV), active.get("started_at"), now.isoformat())
    return jsonify({
        "active": True,
        "session": {**active, "elapsed_seconds": round(elapsed, 1), "emails_sent": sent},
        "history": list(reversed(history)),
    })


@app.route("/api/session/history")
def session_history():
    return jsonify({"history": list(reversed(_load_session_history()))})


@app.route("/api/stats")
def stats():
    sent_rows = read_csv_dicts(SENT_LOG_CSV)
    lead_rows = read_csv_dicts(LEADS_CSV)
    configured_for_warmup = get_configured_accounts()
    env_values = _parse_env(_read_env_lines())
    try:
        account_cfg = _ensure_account_controls()
        default_limit = int(env_values.get("DEFAULT_ACCOUNT_DAILY_LIMIT", "5") or 5)
        global_max = int(env_values.get("GLOBAL_DAILY_MAX", "1000") or 1000)
        warmup = Warmup(WARMUP_FILE, [a["email"] for a in configured_for_warmup],
                        default_limit, 0, global_max, account_cfg).load()
    except (TypeError, ValueError):
        warmup = read_json(WARMUP_FILE, {"date": "", "accounts": {}})
    bounces   = read_json(BOUNCES_FILE, {"bounced": 0, "attempted": 0})
    health    = read_json(HEALTH_FILE, {})
    acct_errs = read_json(STATE_DIR / "account_errors.json", {"errors": {}})
    routing_skips = read_json(STATE_DIR / "routing_skips.json", {"skipped": 0})
    configured_accounts = configured_for_warmup

    total_sent  = len(sent_rows)
    replies     = sum(1 for r in sent_rows if r.get("Reply Status") not in ("None", "", None))

    accounts = compute_account_stats(sent_rows, warmup.get("accounts", {}), configured_accounts,
                                       int(env_values.get("DEFAULT_ACCOUNT_DAILY_LIMIT", "5") or 5))
    routing = _ensure_account_controls()
    for a in accounts:
        s = routing.get(a["email"], {"active": True, "niches": []})
        a["active"] = s.get("active", True)
        a["niches"] = s.get("niches", [])
        a["errors"] = acct_errs.get("errors", {}).get(a["email"], 0)
        a["warmup_enabled"] = bool(s.get("warmup_enabled", False))
        a["warmup_increment"] = int(s.get("warmup_increment", 0))
        a["warmup_max"] = int(s.get("warmup_max", a["daily_limit"]))
        if not a["active"]:
            a["health"] = "paused"
        elif a["errors"]:
            a["health"] = "error"
        elif a["sent_today"] >= a["daily_limit"]:
            a["health"] = "at_daily_limit"
        elif a["warmup_enabled"]:
            a["health"] = "warming_up"
        else:
            a["health"] = "ready"

    global_max = int(env_values.get("GLOBAL_DAILY_MAX", "1000") or 1000)
    total_account_capacity = sum(a["daily_limit"] for a in accounts)
    remaining_account_capacity = sum(max(0, a["daily_limit"] - a["sent_today"]) for a in accounts if a["active"])
    global_remaining = max(0, global_max - sum(a["sent_today"] for a in accounts))
    strategy = env_values.get("SENDING_STRATEGY", "auto").lower()

    down_providers = [p for p, v in health.items() if v.get("status") == "down"]

    # Last-7-day send counts power the performance chart in the replacement UI.
    from datetime import datetime, timedelta
    today = datetime.now().date()
    daily_counts = []
    for offset in range(6, -1, -1):
        target = today - timedelta(days=offset)
        count = 0
        for row in sent_rows:
            raw = str(row.get("Date Sent") or "")
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
            except ValueError:
                try:
                    parsed = datetime.strptime(raw[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
            if parsed == target:
                count += 1
        daily_counts.append(count)

    return jsonify({
        "running": agent_running(),
        "dry_run": env_values.get("DRY_RUN", "true").lower() != "false",
        "leads_loaded": len(lead_rows),
        "total_sent": total_sent,
        "replies": replies,
        "reply_rate": round(replies / total_sent * 100, 1) if total_sent else 0,
        "bounce_rate": round(bounces["bounced"] / bounces["attempted"] * 100, 1) if bounces.get("attempted") else 0,
        "sent_today": sum(a["sent_today"] for a in accounts),
        "daily_limit": min(global_max, total_account_capacity),
        "global_daily_max": global_max,
        "account_capacity": total_account_capacity,
        "remaining_capacity": min(global_remaining, remaining_account_capacity),
        "sending_strategy": strategy,
        "accounts": accounts,
        "down_providers": down_providers,
        "routing_skips": routing_skips.get("skipped", 0),
        "recent": list(reversed(sent_rows[-15:])),
        "daily_counts": daily_counts,
    })


@app.route("/api/logs")
def logs():
    return jsonify({"lines": read_log_tail(80)})


@app.route("/api/health")
def health():
    accounts = get_configured_accounts()
    return jsonify({
        "ok": True,
        "running": agent_running(),
        "accounts_configured": len(accounts),
        "leads_file_present": LEADS_CSV.exists(),
        "ai_keys_configured": any(_parse_env(_read_env_lines()).get(k, "").strip() for k in ("GROQ_API_KEYS", "OPENROUTER_API_KEYS", "AGNES_API_KEYS", "POIXE_API_KEYS")),
    })


@app.route("/api/start", methods=["POST"])
def start_agent():
    with START_LOCK:
        if agent_running():
            return jsonify({"ok": False, "msg": "Already running"})
        configured_accounts = get_configured_accounts()
        if not configured_accounts:
            return jsonify({"ok": False, "msg": "Configure at least one Gmail account before starting the agent."})
        routing = _ensure_account_controls()
        if not any(routing.get(a["email"], {"active": True}).get("active", True) for a in configured_accounts):
            return jsonify({"ok": False, "msg": "All Gmail accounts are paused. Activate at least one account before starting the agent."})
        try:
            cfg = Config()
            cfg.validate()
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})
        log_handle = None
        try:
            log_handle = open(LOG_FILE, "a", encoding="utf-8")
            popen_kwargs = {}
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen([sys.executable, str(BASE_DIR / "agent.py")], stdout=log_handle,
                                     stderr=subprocess.STDOUT, cwd=str(BASE_DIR.resolve()), **popen_kwargs)
            PID_FILE.write_text(str(proc.pid))
            started_at = datetime.now(timezone.utc)
            atomic_write_json(SESSION_FILE, {
                "id": int(started_at.timestamp() * 1000),
                "started_at": started_at.isoformat(),
                "pid": proc.pid,
            })
            capture_message("UMR Outreach agent session started", level="info")
            return jsonify({"ok": True, "pid": proc.pid, "started_at": started_at.isoformat()})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})
        finally:
            if log_handle is not None:
                log_handle.close()


def _pid_alive(pid):
    try:
        os.kill(pid, 0); return True
    except Exception:
        return False


@app.route("/api/stop", methods=["POST"])
def stop_agent():
    pid = agent_pid()
    if not pid:
        return jsonify({"ok": False, "msg": "Not running"})
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        for _ in range(10):
            if not _pid_alive(pid):
                break
            time.sleep(0.5)
        else:
            if sys.platform == "win32":
                subprocess.call(["taskkill", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        session = _finalize_active_session(reason="stopped")
        capture_message("UMR Outreach agent session stopped", level="info")
        return jsonify({"ok": True, "session": session})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ── UI — clean, light, Apple-inspired ───────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UMR Outreach</title>
<style>
:root{
  --bg:#050708;--panel:#0b0e10;--panel2:#0f1315;--panel3:#13181a;--line:#202629;--line2:#2c3336;
  --text:#f4f7f6;--muted:#98a19f;--muted2:#66706d;--green:#3bd57a;--green2:#1da85a;--red:#ff5f5f;--amber:#f4b63e;
  --shadow:0 18px 60px rgba(0,0,0,.35);--radius:18px;
}
*{box-sizing:border-box}html{background:var(--bg);scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 15% -20%,rgba(59,213,122,.06),transparent 30%),radial-gradient(circle at 85% -5%,rgba(59,213,122,.035),transparent 26%),linear-gradient(180deg,#050708 0%,#07090a 100%);color:var(--text);font-family:Inter,'Segoe UI',system-ui,sans-serif;font-size:13px;-webkit-font-smoothing:antialiased}button,input{font:inherit}button{border:0}.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.hidden{display:none!important}
.topbar{height:72px;position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;padding:0 26px;border:1px solid rgba(255,255,255,.07);border-radius:0 0 16px 16px;background:rgba(5,7,8,.84);backdrop-filter:blur(18px)}
.brand{display:flex;align-items:center;gap:11px;min-width:260px}.brand-logo{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;background:linear-gradient(145deg,#1d2527,#0c1012);border:1px solid var(--line2);box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}.brand-logo svg{width:19px;height:19px}.brand-title{font-weight:700;font-size:16px;letter-spacing:-.02em}.brand-sub{font-size:10px;color:var(--muted2);margin-top:3px}
.nav{display:flex;align-items:center;gap:2px;padding:4px;background:#0a0d0f;border:1px solid var(--line);border-radius:14px}.nav-btn{display:flex;align-items:center;gap:8px;padding:9px 16px;border-radius:10px;background:transparent;color:var(--muted);cursor:pointer;transition:.18s}.nav-btn:hover{color:var(--text);background:rgba(255,255,255,.03)}.nav-btn.active{color:var(--text);background:linear-gradient(180deg,#17201b,#111715);box-shadow:inset 0 0 0 1px rgba(59,213,122,.23)}.nav-btn svg{width:16px;height:16px}.nav-btn.active svg{color:var(--green)}
.top-right{min-width:260px;display:flex;justify-content:flex-end;align-items:center;gap:10px}.live-pill,.mode-pill{display:flex;align-items:center;gap:7px;padding:8px 13px;border:1px solid var(--line);border-radius:999px;background:#0b0e10;color:var(--muted);font-size:11px}.live-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 12px rgba(59,213,122,.7)}.avatar{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:linear-gradient(145deg,#283034,#121719);border:1px solid var(--line2);font-weight:700}.mode-pill.dry{color:var(--green);background:rgba(59,213,122,.06);border-color:rgba(59,213,122,.2)}.mode-pill.live{color:var(--amber);background:rgba(244,182,62,.06);border-color:rgba(244,182,62,.2)}
main{max-width:1540px;margin:0 auto;padding:25px 26px 60px}.view{display:none}.view.active{display:block;animation:fadeIn .25s ease}@keyframes fadeIn{from{opacity:.6;transform:translateY(5px)}to{opacity:1;transform:none}}
.kpi-grid{display:grid;grid-template-columns:1.18fr repeat(4,1fr);gap:12px}.card{background:linear-gradient(180deg,rgba(15,19,21,.96),rgba(9,12,13,.98));border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 12px 40px rgba(0,0,0,.18)}.kpi{min-height:162px;padding:18px;position:relative;overflow:hidden}.kpi:after{content:"";position:absolute;inset:auto -30% -60% 20%;height:120px;background:radial-gradient(circle,rgba(59,213,122,.06),transparent 70%);pointer-events:none}.kpi-label{display:flex;align-items:center;gap:8px;color:#d9dfdd;font-size:11px;text-transform:uppercase;letter-spacing:.07em}.kpi-label svg{width:16px;color:#cbd3d0}.kpi-main{margin-top:20px;display:flex;align-items:flex-end;justify-content:space-between;gap:12px}.kpi-value{font-size:31px;font-weight:700;letter-spacing:-.04em}.kpi-sub{margin-top:7px;color:var(--muted);font-size:10px}.trend{display:flex;align-items:center;gap:4px;font-size:10px;margin-top:7px}.trend.up{color:var(--green)}.trend.down{color:var(--red)}.mini-chart{width:84px;height:34px}.mini-chart path{fill:none;stroke:var(--green);stroke-width:2.3;stroke-linecap:round;stroke-linejoin:round}.today-ring{width:116px;height:116px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--green) var(--pct,0%),#262c2e 0);position:relative}.today-ring:after{content:"";position:absolute;width:88px;height:88px;border-radius:50%;background:#0a0d0f;border:1px solid #202628}.ring-center{position:absolute;z-index:2;text-align:center}.ring-center strong{font-size:20px}.ring-center span{display:block;color:var(--muted);font-size:9px;margin-top:3px}
.agent-control{margin-top:12px;padding:15px 17px;display:flex;align-items:center;justify-content:space-between;gap:16px;border-color:rgba(59,213,122,.16);background:linear-gradient(180deg,rgba(13,20,16,.98),rgba(9,13,12,.98))}.agent-control-left{min-width:0}.control-label{font-size:9px;letter-spacing:.11em;color:var(--green);font-weight:800}.control-title{font-size:15px;font-weight:700;margin-top:4px}.control-sub{font-size:10px;color:var(--muted);margin-top:4px;line-height:1.45}.agent-control-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}.control-state{display:inline-flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--line);border-radius:10px;font-size:10px;color:var(--muted);background:#0a0d0e}.state-dot{width:7px;height:7px;border-radius:50%;background:var(--muted2)}.state-dot.live{background:var(--green);box-shadow:0 0 12px rgba(59,213,122,.55)}.control-btn{white-space:nowrap}.btn.danger{color:#ff8a8a;border-color:rgba(255,95,95,.24);background:rgba(255,95,95,.055)}.btn.danger:hover{background:rgba(255,95,95,.09)}
.ov-grid{display:grid;grid-template-columns:1.05fr 1.25fr 1.05fr;gap:14px;margin-top:14px}.ov-card{padding:17px}.card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px}.card-title{font-size:12px;font-weight:700;letter-spacing:.02em}.card-link{color:var(--green);font-size:11px;cursor:pointer}.activity-list{display:grid;gap:8px}.activity{display:grid;grid-template-columns:35px 1fr auto;gap:10px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}.upload-box{padding:12px;border:1px dashed var(--line2);border-radius:14px;background:#0a0d0f}.upload-help{font-size:10px;color:var(--muted);line-height:1.5}.upload-help b{color:var(--text)}.upload-actions{display:flex;gap:8px;align-items:center;margin-top:10px}.upload-actions input[type=file]{flex:1;min-width:0;padding:8px;border:1px solid var(--line);border-radius:10px;background:#080b0c;color:var(--muted);font-size:9px}.upload-actions .btn{white-space:nowrap}.upload-msg{min-height:14px;margin-top:7px;font-size:9px;color:var(--muted)}.activity:first-child{border-top:0}.act-icon{width:35px;height:35px;border-radius:11px;background:#111618;border:1px solid var(--line);display:grid;place-items:center;color:var(--green)}.act-icon svg{width:17px}.act-main{min-width:0}.act-title{font-size:11px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.act-meta{font-size:9px;color:var(--muted2);margin-top:3px}.act-time{font-size:9px;color:var(--muted2);white-space:nowrap}
.account-list{display:grid;gap:2px}.account-row{display:flex;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line)}.account-row:first-child{border-top:0}.gmail-icon{width:27px;height:27px;border-radius:8px;background:#fff;display:grid;place-items:center;flex:none}.gmail-icon svg{width:18px;height:18px}.account-row-main{min-width:0;flex:1}.account-row-name{font-size:10.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.account-row-email{font-size:9px;color:var(--muted2);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.status-mini{font-size:9px;color:var(--green);margin-right:4px}.switch{position:relative;width:38px;height:22px;flex:none}.switch input{opacity:0;width:0;height:0}.slider{position:absolute;inset:0;border-radius:999px;background:#252b2d;border:1px solid var(--line2);cursor:pointer;transition:.18s}.slider:before{content:"";position:absolute;left:2px;top:2px;width:16px;height:16px;border-radius:50%;background:#d3d9d7;transition:.18s}.switch input:checked+.slider{background:rgba(59,213,122,.2);border-color:rgba(59,213,122,.36)}.switch input:checked+.slider:before{transform:translateX(16px);background:var(--green)}
.terminal{height:276px;overflow:auto;background:#060809;border-radius:14px;border:1px solid #161b1d;padding:14px;color:#a6b2ad;font:10px/1.8 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.log-line{white-space:pre-wrap}.log-line:before{content:"•";color:var(--green);margin-right:8px}.empty{color:var(--muted2);text-align:center;padding:24px 10px;font-size:11px}.provider-row{display:grid;grid-template-columns:34px 1fr auto;align-items:center;gap:10px;padding:13px 0;border-top:1px solid var(--line)}.provider-row:first-child{border-top:0}.provider-icon{width:34px;height:34px;border-radius:10px;background:#101517;border:1px solid var(--line2);display:grid;place-items:center;font-weight:700;color:#dce2df}.provider-name{font-size:11px;font-weight:600}.provider-count{font-size:9px;color:var(--muted2);margin-top:3px}.healthy{display:flex;align-items:center;gap:6px;color:var(--green);font-size:10px}.healthy-dot{width:7px;height:7px;border-radius:50%;background:var(--green)}
.performance{grid-column:1 / span 2;padding:17px}.perf-top{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin-bottom:16px}.perf-stat{padding-right:22px;border-right:1px solid var(--line)}.perf-stat:not(:first-child){padding-left:22px}.perf-stat:last-child{border-right:0}.perf-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}.perf-value{font-size:20px;font-weight:700;margin-top:10px}.perf-change{font-size:9px;margin-top:4px}.chart{height:180px;border-top:1px solid var(--line);padding-top:12px}.chart svg{width:100%;height:100%}.gridline{stroke:rgba(255,255,255,.06);stroke-width:1}.line{fill:none;stroke:var(--green);stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}.area{fill:url(#fade)}.axis-label{fill:#66706d;font-size:9px}
.section-head{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;margin:5px 0 19px}.page-title{font-size:24px;font-weight:700;letter-spacing:-.03em}.page-sub{margin-top:5px;color:var(--muted);font-size:11px}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.search{width:240px;padding:11px 13px;border-radius:12px;border:1px solid var(--line2);background:#0b0f10;color:var(--text);outline:none}.search:focus{border-color:rgba(59,213,122,.35);box-shadow:0 0 0 3px rgba(59,213,122,.06)}.btn{padding:10px 14px;border-radius:11px;background:#111617;border:1px solid var(--line2);color:var(--text);cursor:pointer;font-weight:600;font-size:11px;transition:.15s}.btn:hover{transform:translateY(-1px);background:#151b1d}.btn.green{background:linear-gradient(180deg,#3fdc80,#29b866);border-color:transparent;color:#05130a;box-shadow:0 8px 24px rgba(59,213,122,.1)}.btn.danger{color:#ff9c9c;border-color:rgba(255,95,95,.25);background:rgba(255,95,95,.05)}.btn:disabled{opacity:.4;cursor:not-allowed;transform:none}
.accounts-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.acct-card{padding:16px;min-width:0}.acct-head{display:flex;align-items:flex-start;gap:11px}.acct-avatar{width:42px;height:42px;border-radius:12px;background:#fff;display:grid;place-items:center;flex:none}.acct-avatar svg{width:25px;height:25px}.acct-id{min-width:0;flex:1}.acct-name{font-size:12px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.acct-email{font-size:10px;color:var(--muted);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.badge-primary,.badge-status{display:inline-flex;align-items:center;gap:5px;padding:4px 7px;border-radius:999px;font-size:8px;margin-top:6px;background:#121719;border:1px solid var(--line2);color:var(--muted)}.badge-primary{color:#bde7cf;border-color:rgba(59,213,122,.18);background:rgba(59,213,122,.055)}.badge-status.active{color:var(--green)}.badge-status.paused{color:var(--amber)}.acct-top-actions{display:flex;align-items:center;gap:8px}.dots{width:28px;height:28px;border-radius:9px;background:#0c1011;border:1px solid var(--line);color:var(--muted);cursor:pointer}.acct-metrics{display:grid;grid-template-columns:repeat(5,1fr);margin-top:14px;border:1px solid var(--line);border-radius:14px;overflow:hidden}.metric{padding:11px 8px;border-right:1px solid var(--line);min-width:0}.metric:last-child{border-right:0}.metric-label{font-size:8px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}.metric-value{font-size:17px;margin-top:11px}.metric-change{font-size:8px;margin-top:4px;color:var(--green);white-space:nowrap}.metric-change.bad{color:var(--red)}.metric-ring{width:58px;height:58px;border-radius:50%;display:grid;place-items:center;position:relative;margin-top:6px;background:conic-gradient(var(--green) var(--pct,0%),#23292b 0)}.metric-ring:after{content:"";position:absolute;width:43px;height:43px;border-radius:50%;background:#0f1315}.metric-ring span{position:relative;z-index:2;font-size:11px}.limit{margin-top:14px}.limit-row{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:7px}.progress{height:6px;border-radius:99px;background:#1c2224;overflow:hidden}.progress>span{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--green2),var(--green))}.niche-head{display:flex;justify-content:space-between;align-items:center;margin-top:15px;font-size:10px}.niche-title{color:#d6ddda}.edit-link{font-size:10px;color:var(--text);padding:6px 9px;border:1px solid var(--line2);border-radius:9px;background:#0b0f10;cursor:pointer}.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}.chip{font-size:9px;padding:6px 8px;border-radius:999px;background:#101517;border:1px solid var(--line);color:var(--muted);white-space:nowrap}.chip.more{color:#b8c0bd}.acct-error{font-size:9px;color:var(--amber);margin-top:10px}.pagination{display:flex;justify-content:center;align-items:center;gap:8px;margin:20px 0}.page-btn{width:34px;height:34px;border-radius:10px;background:#0d1112;color:var(--muted);border:1px solid var(--line);cursor:pointer}.page-btn.active{color:#06130a;background:var(--green);border-color:var(--green)}
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;max-width:1000px}.settings-card{padding:18px}.settings-card h3{font-size:13px}.hint{font-size:10px;color:var(--muted);line-height:1.6;margin:5px 0 16px}.field{margin-top:13px}.field label{display:block;color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}.field input{width:100%;padding:10px 11px;border-radius:10px;border:1px solid var(--line);background:#080b0c;color:var(--text);outline:none}.field input:focus{border-color:rgba(59,213,122,.32)}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:10px}.toggle-line{display:flex;align-items:center;justify-content:space-between;padding:13px;border:1px solid var(--line);border-radius:13px;background:#0a0d0e}.toggle-title{font-size:11px;font-weight:600}.toggle-sub{font-size:9px;color:var(--muted);margin-top:4px}.provider-editor{border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:11px}.provider-editor-head{display:flex;justify-content:space-between;align-items:center}.key-row{display:flex;align-items:center;justify-content:space-between;gap:8px;background:#0a0d0f;border:1px solid var(--line);border-radius:9px;padding:8px;margin-top:8px}.key-label{font:9px ui-monospace,monospace;color:#87918e}.new-key{display:flex;gap:7px;margin-top:8px}.new-key input{flex:1}.savebar{grid-column:1/-1;display:flex;align-items:center;gap:10px;position:sticky;bottom:12px;padding:11px 13px;border:1px solid var(--line2);border-radius:13px;background:rgba(9,12,13,.9);backdrop-filter:blur(14px)}#settingsMsg{font-size:10px;color:var(--muted)}
.modal-backdrop{position:fixed;inset:0;z-index:80;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:20px}.modal-backdrop.open{display:flex}.modal{width:min(520px,100%);background:#0b0f10;border:1px solid var(--line2);border-radius:18px;box-shadow:0 30px 100px rgba(0,0,0,.55);overflow:hidden}.modal-head{display:flex;justify-content:space-between;gap:15px;padding:18px 20px;border-bottom:1px solid var(--line)}.modal-title{font-size:15px;font-weight:700}.modal-sub{font-size:10px;color:var(--muted);margin-top:4px}.modal-body{padding:18px 20px}.modal-foot{display:flex;justify-content:flex-end;gap:8px;padding:13px 20px;border-top:1px solid var(--line)}.form-msg{min-height:18px;margin-top:9px;font-size:10px;color:var(--amber)}.niche-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.niche-option{display:flex;align-items:center;gap:8px;padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:#0a0d0e;color:var(--muted);font-size:10px;cursor:pointer}.niche-option input{accent-color:var(--green)}
.session-card{padding:16px;margin-top:14px}.session-grid{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:10px;margin-top:14px}.session-label{font-size:8px;color:var(--muted);letter-spacing:.08em}.session-time{font-size:27px;font-weight:700;margin-top:5px}.session-number{font-size:20px;font-weight:700;margin-top:7px}.session-note{margin-top:12px;color:var(--muted);font-size:10px}.session-history-modal{max-width:720px}.history-row{display:grid;grid-template-columns:1.2fr .8fr .8fr .7fr;gap:10px;padding:11px 0;border-top:1px solid var(--line);font-size:10px}.history-row:first-child{border-top:0}.history-muted{color:var(--muted)}
.toast{position:fixed;right:20px;bottom:20px;z-index:100;background:#101618;border:1px solid var(--line2);padding:11px 14px;border-radius:12px;color:var(--text);font-size:11px;box-shadow:var(--shadow);display:none}.toast.show{display:block}
@media(max-width:1200px){.kpi-grid{grid-template-columns:repeat(3,1fr)}.ov-grid{grid-template-columns:1fr 1fr}.performance{grid-column:1/-1}.accounts-grid{grid-template-columns:repeat(2,1fr)}.brand{min-width:220px}.top-right{min-width:220px}}
@media(max-width:820px){.topbar{padding:0 14px}.brand-sub{display:none}.brand{min-width:0}.top-right{min-width:0}.avatar{display:none}.nav-btn{padding:9px 10px}.nav-btn span{display:none}main{padding:18px 14px 40px}.kpi-grid{grid-template-columns:1fr 1fr}.ov-grid{grid-template-columns:1fr}.accounts-grid{grid-template-columns:1fr}.settings-grid{grid-template-columns:1fr}.perf-top{grid-template-columns:1fr 1fr;gap:12px}.perf-stat{border-right:0;border-bottom:1px solid var(--line);padding:0 0 10px!important}.perf-stat:nth-child(odd){padding-right:10px!important}.perf-stat:nth-child(even){padding-left:10px!important}.toolbar,.section-head{align-items:stretch}.search{width:100%}.today-ring{width:92px;height:92px}.today-ring:after{width:68px;height:68px}}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand"><div class="brand-logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 13c0-5 3-8 8-8s8 3 8 8"/><path d="M8 12v5"/><path d="M16 12v5"/><path d="M8 17c2 2 6 2 8 0"/><path d="M4 14 2 12l2-3"/><path d="M20 14l2-2-2-3"/></svg></div><div><div class="brand-title">UMR Outreach</div><div class="brand-sub">Intelligent outreach. Real results.</div></div></div>
  <nav class="nav"><button class="nav-btn active" data-view="overview"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="8" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/></svg><span>Overview</span></button><button class="nav-btn" data-view="accounts"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg><span>Accounts</span></button><button class="nav-btn" data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1-1.8 1.8-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21h-2.6v-.2a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1-1.8-1.8.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3v-2.6h.2a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1 1.8-1.8.1.1a1.6 1.6 0 0 0 1.8.3 1.6 1.6 0 0 0 1-1V3h2.6v.2a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1 1.8 1.8-.1.1a1.6 1.6 0 0 0-.3 1.8 1.6 1.6 0 0 0 1.8.3 1.6 1.6 0 0 0 1 1.5H21V13h-.2a1.6 1.6 0 0 0-1.4 2Z"/></svg><span>Settings</span></button></nav>
  <div class="top-right"><span class="mode-pill" id="modeBadge">Dry run</span><span class="live-pill"><span class="live-dot" id="statusDot"></span><span id="statusText">Stopped</span></span><div class="avatar">UMR</div></div>
</header>
<main>
<section class="view active" id="view-overview">
  <div class="kpi-grid">
    <div class="card kpi"><div class="kpi-label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m21 3-7.3 18-3.1-8L3 10 21 3Z"/><path d="m10.6 13.4 4.8-4.8"/></svg>Sent today</div><div class="kpi-main"><div><div class="kpi-value mono" id="m-today">0 / 0</div><div class="kpi-sub">of daily limit</div></div><div class="today-ring" id="todayRing"><div class="ring-center"><strong id="todayPct">0%</strong><span>utilized</span></div></div></div></div>
    <div class="card kpi"><div class="kpi-label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6h16v12H4z"/><path d="m4 7 8 6 8-6"/></svg>Total sent</div><div class="kpi-main"><div><div class="kpi-value mono" id="m-sent">0</div><div class="trend up" id="sentTrend">↑ live</div></div><svg class="mini-chart" viewBox="0 0 84 34"><path d="M2 28 C12 20,14 27,22 18 S33 26,42 16 S53 24,61 11 S72 20,82 5"/></svg></div><div class="kpi-sub">All accounts</div></div>
    <div class="card kpi"><div class="kpi-label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 5h16v10H4z"/><path d="m7 19 3-4h4l3 4"/></svg>Reply rate</div><div class="kpi-main"><div><div class="kpi-value mono" id="m-rate">0%</div><div class="trend up">↑ response</div></div><svg class="mini-chart" viewBox="0 0 84 34"><path d="M2 28 C14 27,15 17,23 22 S36 23,42 16 S51 21,60 13 S73 18,82 4"/></svg></div><div class="kpi-sub" id="m-rate-sub">0 replies</div></div>
    <div class="card kpi"><div class="kpi-label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m3 12 4-4 4 4 4-6 6 6"/><path d="M5 19h14"/></svg>Bounce rate</div><div class="kpi-main"><div><div class="kpi-value mono" id="m-bounce">0%</div><div class="trend down">↓ delivery</div></div><svg class="mini-chart" viewBox="0 0 84 34"><path d="M2 8 C12 17,17 4,27 12 S40 10,49 18 S64 12,82 26" style="stroke:var(--amber)"/></svg></div><div class="kpi-sub">Delivery health</div></div>
    <div class="card kpi"><div class="kpi-label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="9" cy="8" r="3"/><path d="M3 20c.7-3.3 2.7-5 6-5s5.3 1.7 6 5"/><circle cx="18" cy="10" r="2"/></svg>Leads loaded</div><div class="kpi-main"><div><div class="kpi-value mono" id="m-leads">0</div><div class="trend up">↑ campaign</div></div><svg class="mini-chart" viewBox="0 0 84 34"><path d="M2 30 C12 22,18 29,24 21 S35 24,43 17 S55 19,63 12 S72 16,82 5"/></svg></div><div class="kpi-sub">Across all accounts</div></div>
  </div>
  <div id="banner"></div>
  <div class="card session-card" id="sessionCard"><div class="card-head"><div><div class="card-title">SESSION</div><div class="muted" style="font-size:9px">Live sending session</div></div><button class="btn" id="viewHistoryBtn" onclick="openSessionHistory()">View history</button></div><div class="session-grid"><div><div class="session-label">TIME</div><div class="session-time mono" id="sessionTimer">00:00:00</div></div><div><div class="session-label">EMAILS SENT</div><div class="session-number mono" id="sessionSent">0</div></div><div><div class="session-label">AVG TIME / EMAIL</div><div class="session-number mono" id="sessionRate">—</div></div></div><div class="session-note" id="sessionNote">No active session.</div></div>

<div class="card agent-control" id="agentControl">
    <div class="agent-control-left">
      <div class="control-label">AGENT CONTROL</div>
      <div class="control-title" id="agentControlTitle">System is stopped</div>
      <div class="control-sub" id="agentControlSub">Start the agent to process your loaded leads using the active Gmail accounts.</div>
    </div>
    <div class="agent-control-actions">
      <span class="control-state" id="agentControlState"><span class="state-dot" id="agentStateDot"></span><span id="agentStateLabel">Stopped</span></span>
      <button class="btn green control-btn" id="startAgentBtn" onclick="startAgent()">▶ Start sending</button>
      <button class="btn danger control-btn" id="stopAgentBtn" onclick="stopAgent()" disabled>■ Stop sending</button>
    </div>
  </div>
  <div class="ov-grid">
    <div>
      <div class="card ov-card"><div class="card-head"><div class="card-title">ACCOUNTS</div><span class="card-link" onclick="openAccountModal()">＋ Add</span></div><div class="account-list" id="overviewAccounts"><div class="empty">Loading…</div></div><div class="card-link" style="margin-top:10px" onclick="setView('accounts')">View all accounts →</div></div>
      <div class="card ov-card" style="margin-top:14px"><div class="card-head"><div class="card-title">UPLOAD LEADS</div><span class="muted" style="font-size:9px">CSV</span></div><div class="upload-box"><div class="upload-help">Upload a new prospect list. Required columns: <b>Name</b>, <b>Email</b>, and <b>Niche</b>.</div><div class="upload-actions"><input type="file" id="leadsFile" accept=".csv,text/csv"><button class="btn green" id="uploadLeadsBtn" onclick="uploadLeads()">Upload CSV</button></div><div id="uploadMsg" class="upload-msg"></div></div></div>
      <div class="card ov-card" style="margin-top:14px"><div class="card-head"><div class="card-title">AI PROVIDER STATUS</div><span class="card-link" onclick="setView('settings')">Manage keys →</span></div><div id="providerList"><div class="empty">Loading…</div></div></div>
    </div>
    <div>
      <div class="card ov-card"><div class="card-head"><div class="card-title">RECENT ACTIVITY</div><span class="muted" style="font-size:9px">Latest sends</span></div><div class="activity-list" id="recentActivity"><div class="empty">No activity yet.</div></div></div>
      <div class="card performance" style="margin-top:14px"><div class="card-head"><div class="card-title">PERFORMANCE OVERVIEW</div><span class="muted" style="font-size:9px">Last 7 days</span></div><div class="perf-top"><div class="perf-stat"><div class="perf-label">Sent</div><div class="perf-value mono" id="perfSent">0</div><div class="perf-change" style="color:var(--green)">Live total</div></div><div class="perf-stat"><div class="perf-label">Replies</div><div class="perf-value mono" id="perfReplies">0</div><div class="perf-change" style="color:var(--muted)">From sent log</div></div><div class="perf-stat"><div class="perf-label">Reply rate</div><div class="perf-value mono" id="perfRate">0%</div><div class="perf-change" style="color:var(--green)">Quality signal</div></div><div class="perf-stat"><div class="perf-label">Bounce rate</div><div class="perf-value mono" id="perfBounce">0%</div><div class="perf-change" style="color:var(--amber)">Delivery signal</div></div></div><div class="chart"><svg viewBox="0 0 760 180" preserveAspectRatio="none"><defs><linearGradient id="fade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3bd57a" stop-opacity=".23"/><stop offset="1" stop-color="#3bd57a" stop-opacity="0"/></linearGradient></defs><g id="perfGrid"></g><path id="perfArea" class="area" d=""></path><path id="perfLine" class="line" d=""></path><g id="perfLabels"></g></svg></div></div>
    </div>
    <div>
      <div class="card ov-card"><div class="card-head"><div class="card-title">LIVE LOGS</div><span class="muted" style="font-size:9px">Agent output</span></div><div class="terminal" id="log">Loading…</div><div class="card-link" style="margin-top:10px" onclick="refreshLogs()">Refresh logs →</div></div>
    </div>
  </div>
</section>

<section class="view" id="view-accounts">
  <div class="section-head"><div><div class="page-title">Accounts</div><div class="page-sub">Manage and monitor all your email accounts</div></div><div class="toolbar"><input class="search" id="acctSearch" placeholder="Search accounts…" oninput="renderAccountsPage()"><button class="btn green" onclick="openAccountModal()">＋ Add Account</button></div></div>
  <div class="accounts-grid" id="accountsGrid"><div class="empty">Loading accounts…</div></div>
  <div class="pagination" id="pagination"></div>
</section>

<section class="view" id="view-settings">
  <div class="section-head"><div><div class="page-title">Settings</div><div class="page-sub">Sending behavior, AI providers, and campaign controls</div></div></div>
  <div class="settings-grid">
    <div class="card settings-card"><div class="card-head"><div><div class="card-title">SENDING MODE</div><div class="hint">Switch between safe simulation and real outreach.</div></div></div><div class="toggle-line"><div><div class="toggle-title" id="dryTitle">Dry run</div><div class="toggle-sub" id="drySub">No real emails are sent.</div></div><label class="switch"><input type="checkbox" id="set_DRY_RUN"><span class="slider"></span></label></div><div class="field"><label>Sender name</label><input id="set_IDENTITY_NAME"></div><div class="field"><label>Brand name</label><input id="set_BRAND_NAME"></div><div class="field"><label>Reply-to address</label><input id="set_FORWARD_EMAIL"></div></div>
    <div class="card settings-card"><div class="card-title">GLOBAL SAFETY &amp; STRATEGY</div><div class="hint">The global ceiling protects the whole campaign; individual account limits are managed on each account.</div><div class="two-col"><div class="field"><label>Global maximum daily emails</label><input type="number" min="1" id="set_GLOBAL_DAILY_MAX"></div><div class="field"><label>Sending strategy</label><select id="set_SENDING_STRATEGY"><option value="auto">Auto</option><option value="balanced">Balanced</option><option value="capacity_first">Capacity first</option><option value="round_robin">Round robin</option></select></div></div></div>
    <div class="card settings-card"><div class="card-title">WORKER CONTROLS</div><div class="hint">Bounded concurrency keeps generation fast without allowing the pipeline to run unbounded.</div><div class="two-col"><div class="field"><label>AI generation workers</label><input type="number" min="1" id="set_AI_WORKER_COUNT"></div><div class="field"><label>Send workers</label><input type="number" min="1" id="set_SEND_WORKER_COUNT"></div><div class="field"><label>Pipeline queue size</label><input type="number" min="10" id="set_PIPELINE_QUEUE_SIZE"></div></div></div>
    <div class="card settings-card" style="grid-column:1/-1"><div class="card-title">AI PROVIDER KEYS</div><div class="hint">Existing secrets remain masked. Add or remove keys without exposing stored values.</div><div id="providerEditList"></div></div>
    <div class="savebar"><button class="btn green" onclick="saveAll()">Save changes</button><span id="settingsMsg">Changes are local to this dashboard.</span></div>
  </div>
</section>
</main>

<div class="modal-backdrop" id="accountModal" onclick="if(event.target===this)closeAccountModal()"><div class="modal" role="dialog" aria-modal="true"><div class="modal-head"><div><div class="modal-title">Add Gmail account</div><div class="modal-sub">Verify the mailbox before it joins your sending pool.</div></div><button class="btn" onclick="closeAccountModal()">Close</button></div><div class="modal-body"><div class="field"><label>Account name</label><input id="accountName" placeholder="e.g. UMR Forge Outreach"></div><div class="field"><label>Gmail address</label><input id="accountEmail" type="email" placeholder="you@gmail.com"></div><div class="field"><label>App password</label><input id="accountPassword" type="password" placeholder="16-character Gmail app password"></div><div class="hint">Spaces are removed automatically. The app password is stored locally and is never returned to the browser.</div><div class="form-msg" id="accountFormMsg"></div></div><div class="modal-foot"><button class="btn" onclick="closeAccountModal()">Cancel</button><button class="btn green" id="verifyAddBtn" onclick="verifyAndAddAccount()">Verify &amp; add</button></div></div></div>
<div class="modal-backdrop" id="nicheModal" onclick="if(event.target===this)closeNicheModal()"><div class="modal"><div class="modal-head"><div><div class="modal-title">Target niche / audience</div><div class="modal-sub" id="nicheAccountLabel">Choose routed niches for this account.</div></div><button class="btn" onclick="closeNicheModal()">Close</button></div><div class="modal-body">
<label class="niche-option"><input type="checkbox" id="allNiche" onchange="toggleAllNiches(this.checked)">All niches</label>
<div class="niche-grid" id="nicheGrid"></div>
<div class="field" style="margin-top:14px"><label>Daily sending limit</label><input type="number" min="1" id="accountDailyLimit"></div>
<div class="toggle-line" style="margin-top:8px"><div><div class="toggle-title">Warm-up</div><div class="toggle-sub">Increase this account's limit gradually.</div></div><label class="switch"><input type="checkbox" id="accountWarmup"><span class="slider"></span></label></div>
<div class="two-col" style="margin-top:10px"><div class="field"><label>Daily increment</label><input type="number" min="0" id="accountWarmupIncrement"></div><div class="field"><label>Warm-up maximum</label><input type="number" min="1" id="accountWarmupMax"></div></div>
<div class="form-msg" id="nicheMsg"></div></div>
<div class="modal-foot"><button class="btn" onclick="closeNicheModal()">Cancel</button><button class="btn green" onclick="saveNicheSettings()">Save account settings</button></div></div></div>
<div class="modal-backdrop" id="sessionHistoryModal" onclick="if(event.target===this)closeSessionHistory()"><div class="modal session-history-modal"><div class="modal-head"><div><div class="modal-title">Session history</div><div class="modal-sub">Your last 100 agent sessions.</div></div><button class="btn" onclick="closeSessionHistory()">Close</button></div><div class="modal-body" id="sessionHistoryList"></div></div></div>
<div class="toast" id="toast"></div>

<script>
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
let latestStats=null, settingsData=null, niches=[], currentView='overview', accountPage=1, pageSize=6, nicheAccount=null, accountRequestBusy=false;
function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('show');clearTimeout(toast.t);toast.t=setTimeout(()=>t.classList.remove('show'),2600)}
async function api(url,opts){const r=await fetch(url,opts);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.msg||`Request failed (${r.status})`);return d}
function setView(view){currentView=view;document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id==='view-'+view));document.querySelectorAll('.nav-btn').forEach(b=>b.classList.toggle('active',b.dataset.view===view));history.replaceState(null,'','#'+view);if(view==='overview'){refreshOverview();refreshLogs()}else if(view==='accounts'){refreshAccountsPage()}else loadSettings()}

document.querySelectorAll('.nav-btn').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
window.addEventListener('hashchange',()=>{const h=location.hash.slice(1);if(['overview','accounts','settings'].includes(h))setView(h)});
async function uploadLeads(){const f=$('leadsFile'),btn=$('uploadLeadsBtn'),msg=$('uploadMsg');if(!f)return;const file=f.files?.[0];if(!file){toast('Choose a CSV first');return}if(!file.name.toLowerCase().endsWith('.csv')){toast('Only CSV files are supported');return}const fd=new FormData();fd.append('file',file);if(btn)btn.disabled=true;if(msg)msg.textContent='Uploading…';try{const d=await api('/api/upload_leads',{method:'POST',body:fd});toast(`Loaded ${d.rows} leads`);if(msg)msg.textContent=`Loaded ${d.rows} leads successfully.`;f.value='';await refreshOverview()}catch(e){if(msg)msg.textContent=e.message;toast(e.message)}finally{if(btn)btn.disabled=false}}
function setAgentControls(running,busy=false,action=''){const start=$('startAgentBtn'),stop=$('stopAgentBtn'),title=$('agentControlTitle'),sub=$('agentControlSub'),label=$('agentStateLabel'),dot=$('agentStateDot');if(!start||!stop)return;start.disabled=running||busy;stop.disabled=!running||busy;start.textContent=busy&&action==='start'?'Starting…':(running?'✓ Started':'▶ Start sending');stop.textContent=busy&&action==='stop'?'Stopping…':'■ Stop sending';title.textContent=running?'System is running':'System is stopped';sub.textContent=running?'The outreach worker is processing leads using the currently active Gmail accounts.':'Start the agent to process your loaded leads using the active Gmail accounts.';label.textContent=running?'Started':'Stopped';dot.classList.toggle('live',running)}
async function startAgent(){if($('startAgentBtn')?.disabled)return;setAgentControls(false,true,'start');try{const s=await api('/api/stats');if(!s.dry_run&&!confirm('LIVE OUTREACH is enabled. This will send real emails to real leads. Continue?')){setAgentControls(false,false);return}const d=await api('/api/start',{method:'POST'});if(!d.ok){toast(d.msg||'Could not start agent');setAgentControls(false,false);return}toast('Agent started');await new Promise(r=>setTimeout(r,350));await refreshOverview();await refreshSession()}catch(e){toast(e.message);setAgentControls(false,false)}}
async function stopAgent(){if($('stopAgentBtn')?.disabled)return;setAgentControls(true,true,'stop');try{const d=await api('/api/stop',{method:'POST'});if(!d.ok)toast(d.msg||'Could not stop agent');else if(d.session)toast(`Session complete — ${formatDuration(d.session.duration_seconds)}, ${d.session.emails_sent} email${d.session.emails_sent===1?'':'s'} sent.`);else toast('Stop signal sent');await new Promise(r=>setTimeout(r,350));await refreshOverview();await refreshSession()}catch(e){toast(e.message)}finally{if(latestStats)setAgentControls(!!latestStats.running,false)}}
function updateKpis(d){const util=d.daily_limit?Math.min(100,Math.round(d.sent_today/d.daily_limit*100)):0;$('m-today').textContent=`${d.sent_today} / ${d.daily_limit}`;$('todayPct').textContent=util+'%';$('todayRing').style.setProperty('--pct',util+'%');$('m-sent').textContent=d.total_sent;$('m-rate').textContent=d.reply_rate+'%';$('m-rate-sub').textContent=`${d.replies} replies`;$('m-bounce').textContent=d.bounce_rate+'%';$('m-leads').textContent=d.leads_loaded;$('perfSent').textContent=d.total_sent;$('perfReplies').textContent=d.replies;$('perfRate').textContent=d.reply_rate+'%';$('perfBounce').textContent=d.bounce_rate+'%';$('statusText').textContent=d.running?'Live':'Stopped';$('statusDot').style.background=d.running?'var(--green)':'var(--muted2)';setAgentControls(!!d.running,false);const b=$('modeBadge');b.textContent=d.dry_run?'Dry run':'Live';b.className='mode-pill '+(d.dry_run?'dry':'live');$('banner').innerHTML='';const warns=[];if((d.down_providers||[]).length)warns.push(`⚠ ${d.down_providers.map(esc).join(', ')} provider${d.down_providers.length===1?' is':'s are'} down.`);if(d.routing_skips)warns.push(`⚠ ${d.routing_skips} lead${d.routing_skips===1?'':'s'} skipped because no active account is routed for its niche.`);$('banner').innerHTML=warns.map(x=>`<div class="card" style="margin:12px 0;padding:11px 13px;color:var(--amber);font-size:10px;border-color:rgba(244,182,62,.22);background:rgba(244,182,62,.045)">${x}</div>`).join('');}
function refreshOverview(){Promise.all([api('/api/stats'),refreshSession()]).then(([d])=>{latestStats=d;updateKpis(d);renderOverviewAccounts(d.accounts||[]);renderActivity(d.recent||[]);renderProviders();drawPerformance(d.daily_counts||[])}).catch(e=>toast(e.message))}
const GMAIL_SVG='<svg viewBox="0 0 24 24"><path fill="#ea4335" d="M3 6.5 12 13l9-6.5V18a2 2 0 0 1-2 2h-1V9.6l-6 4.2-6-4.2V20H5a2 2 0 0 1-2-2V6.5Z"/><path fill="#4285f4" d="M3 6.5 12 13l9-6.5V9l-9 6.2L3 9V6.5Z"/></svg>';
function renderOverviewAccounts(accounts){const box=$('overviewAccounts');if(!accounts.length){box.innerHTML='<div class="empty">No Gmail accounts configured.</div>';return}box.innerHTML=accounts.slice(0,6).map((a,i)=>`<div class="account-row"><div class="gmail-icon">${GMAIL_SVG}</div><div class="account-row-main"><div class="account-row-name">${esc(a.name||a.email)}</div><div class="account-row-email">${esc(a.email)}</div></div>${i===0?'<span class="status-mini">Primary</span>':''}<label class="switch"><input type="checkbox" ${a.active?'checked':''} onchange="toggleActive('${encodeURIComponent(a.email)}',this.checked)"><span class="slider"></span></label></div>`).join('')}
function renderActivity(rows){const box=$('recentActivity');if(!rows.length){box.innerHTML='<div class="empty">No sends yet.</div>';return}box.innerHTML=rows.slice(0,7).map(r=>`<div class="activity"><div class="act-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m4 12 6 6L20 6"/></svg></div><div class="act-main"><div class="act-title">Invitation Sent</div><div class="act-meta">to ${esc(r['Sent From']||'—')} · ${esc(r['Niche']||'Unspecified')}</div></div><div class="act-time">${esc(r['Date Sent']||'')}</div></div>`).join('')}
async function renderProviders(){try{const d=await api('/api/settings');const box=$('providerList');box.innerHTML=(d.multi_key_fields||[]).map(f=>{const short=f.label==='OpenRouter'?'OR':f.label[0];const key=(f.key||'').toLowerCase().replace('_api_keys','');const down=(latestStats?.down_providers||[]).includes(key);return `<div class="provider-row"><div class="provider-icon">${esc(short)}</div><div><div class="provider-name">${esc(f.label)}</div><div class="provider-count">${f.count} key${f.count===1?'':'s'}</div></div><div class="healthy" style="${down?'color:var(--red)':''}"><span class="healthy-dot" style="${down?'background:var(--red)':''}"></span>${down?'Down':'Healthy'}</div></div>`}).join('')||'<div class="empty">No AI providers configured.</div>'}catch(e){$('providerList').innerHTML='<div class="empty">Provider status unavailable.</div>'}}
function drawPerformance(vals){const svgWidth=760,svgHeight=180,pad=12;const series=(vals&&vals.length?vals:[0,0,0,0,0,0,0]).slice(-7);while(series.length<7)series.unshift(0);const max=Math.max(3,...series),step=(svgWidth-pad*2)/(series.length-1);const pts=series.map((v,i)=>[pad+i*step,svgHeight-pad-(v/max)*(svgHeight-pad*2-18)]);const line='M '+pts.map(p=>p.join(' ')).join(' L ');const area=line+` L ${svgWidth-pad} ${svgHeight-pad} L ${pad} ${svgHeight-pad} Z`;$('perfLine').setAttribute('d',line);$('perfArea').setAttribute('d',area);const g=$('perfGrid'),labels=$('perfLabels');g.innerHTML='';labels.innerHTML='';[35,75,115].forEach(y=>{const l=document.createElementNS('http://www.w3.org/2000/svg','line');l.setAttribute('x1','0');l.setAttribute('x2',svgWidth);l.setAttribute('y1',y);l.setAttribute('y2',y);l.setAttribute('class','gridline');g.appendChild(l)});series.forEach((_,i)=>{const t=document.createElementNS('http://www.w3.org/2000/svg','text');t.setAttribute('x',pad+i*step);t.setAttribute('y','174');t.setAttribute('text-anchor',i===0?'start':i===series.length-1?'end':'middle');t.setAttribute('class','axis-label');const d=new Date();d.setDate(d.getDate()-(series.length-1-i));t.textContent=d.toLocaleDateString(undefined,{month:'short',day:'numeric'});labels.appendChild(t)})}
function refreshLogs(){api('/api/logs').then(d=>{$('log').innerHTML=(d.lines?.length?d.lines:['No agent log activity yet.']).map(l=>`<div class="log-line">${esc(l)}</div>`).join('');$('log').scrollTop=$('log').scrollHeight}).catch(e=>{$('log').textContent=e.message})}

function refreshAccountsPage(){api('/api/stats').then(d=>{latestStats=d;renderAccountsPage()}).catch(e=>toast(e.message))}
function renderAccountsPage(){const all=latestStats?.accounts||[];const q=($('acctSearch')?.value||'').toLowerCase().trim();const filtered=all.filter(a=>(a.email+' '+(a.name||'')).toLowerCase().includes(q));const totalPages=Math.max(1,Math.ceil(filtered.length/pageSize));if(accountPage>totalPages)accountPage=totalPages;const start=(accountPage-1)*pageSize;const page=filtered.slice(start,start+pageSize);const box=$('accountsGrid');if(!page.length){box.innerHTML='<div class="empty" style="grid-column:1/-1">No accounts match your search.</div>';renderPagination(0);return}const primaryId=all.length?all[0].id:null;box.innerHTML=page.map(a=>accountCard(a,primaryId)).join('');renderPagination(totalPages)}
function accountCard(a,primaryId){const pct=a.daily_limit?Math.min(100,Math.round(a.sent_today/a.daily_limit*100)):0;const niches=a.niches||[];const chips=niches.length?niches.slice(0,3).map(n=>{const x=nichesMap(n);return `<span class="chip">${esc(x)}</span>`}).join(''):`<span class="chip">All niches</span>`;const more=niches.length>3?`<span class="chip more">+${niches.length-3} more</span>`:'';return `<div class="card acct-card"><div class="acct-head"><div class="acct-avatar">${GMAIL_SVG}</div><div class="acct-id"><div class="acct-name">${esc(a.name||a.email)}</div><div class="acct-email">${esc(a.email)}</div><span class="badge-status ${a.active?'active':'paused'}">● ${a.active?(a.health==='warming_up'?'Warming up':a.health==='at_daily_limit'?'At daily limit':a.health==='error'?'Error':'Ready'):'Paused'}</span>${a.id===primaryId?'<span class="badge-primary">Primary</span>':''}</div><div class="acct-top-actions"><label class="switch"><input type="checkbox" ${a.active?'checked':''} onchange="toggleActive('${encodeURIComponent(a.email)}',this.checked)"><span class="slider"></span></label><button class="dots" title="Delete account" onclick="deleteAccount('${a.id}','${encodeURIComponent(a.email)}')">⋯</button></div></div><div class="acct-metrics"><div class="metric"><div class="metric-label">Sent today</div><div class="metric-ring" style="--pct:${pct}%"><span>${esc(a.sent_today)}/${esc(a.daily_limit)}</span></div></div><div class="metric"><div class="metric-label">Total sent</div><div class="metric-value mono">${esc(a.total_sent)}</div><div class="metric-change">Activity</div></div><div class="metric"><div class="metric-label">Reply rate</div><div class="metric-value mono">${esc(a.reply_rate)}%</div><div class="metric-change">Replies</div></div><div class="metric"><div class="metric-label">Bounce rate</div><div class="metric-value mono">—</div><div class="metric-change">See overview</div></div><div class="metric"><div class="metric-label">Leads loaded</div><div class="metric-value mono">${esc(latestStats?.leads_loaded??0)}</div><div class="metric-change">Campaign</div></div></div><div class="limit"><div class="limit-row"><span>Daily Sending Limit</span><span>${esc(a.daily_limit)} emails</span></div><div class="progress"><span style="width:${pct}%"></span></div><div class="limit-row" style="margin-top:6px"><span>${esc(a.sent_today)} sent</span><span>${Math.max(0,a.daily_limit-a.sent_today)} remaining</span></div></div><div class="niche-head"><span class="niche-title">Target Niche / Audience</span><button class="edit-link" onclick="openNicheModal('${encodeURIComponent(a.email)}')">✎ Edit</button></div><div class="chips">${chips}${more}</div>${a.errors?`<div class="acct-error">⚠ ${esc(a.errors)} failed sends — check this mailbox.</div>`:''}</div>`}
function renderPagination(totalPages){const box=$('pagination');if(totalPages<=1){box.innerHTML='';return}box.innerHTML=`<button class="page-btn" onclick="accountPage=Math.max(1,accountPage-1);renderAccountsPage()">‹</button>${Array.from({length:totalPages},(_,i)=>`<button class="page-btn ${i+1===accountPage?'active':''}" onclick="accountPage=${i+1};renderAccountsPage()">${i+1}</button>`).join('')}<button class="page-btn" onclick="accountPage=Math.min(${totalPages},accountPage+1);renderAccountsPage()">›</button>`}
function nichesMap(id){const x=niches.find(n=>n[0]===id);return x?x[1]:id}
async function toggleActive(encoded,active){const email=decodeURIComponent(encoded);const a=(latestStats?.accounts||[]).find(x=>x.email===email);try{await api('/api/account_settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,active,niches:a?.niches||[]})});toast(active?'Account activated':'Account paused');refreshOverview();refreshAccountsPage()}catch(e){toast(e.message)}}
async function deleteAccount(id,encoded){const email=decodeURIComponent(encoded);if(!confirm(`Remove ${email} from the sending pool?`))return;try{const d=await api('/api/accounts/'+encodeURIComponent(id),{method:'DELETE'});if(!d.ok)throw new Error(d.msg||'Could not remove account');toast('Account removed');refreshOverview();refreshAccountsPage()}catch(e){toast(e.message)}}
function openAccountModal(){$('accountModal').classList.add('open');$('accountFormMsg').textContent='';$('accountName').value='';$('accountEmail').value='';$('accountPassword').value='';$('accountName').focus()}
function closeAccountModal(){$('accountModal').classList.remove('open')}
async function verifyAndAddAccount(){if(accountRequestBusy)return;const name=$('accountName').value.trim(),email=$('accountEmail').value.trim(),pwd=$('accountPassword').value.trim();const msg=$('accountFormMsg');if(!name||!email||!pwd){msg.textContent='Account name, Gmail address, and app password are required.';return}accountRequestBusy=true;$('verifyAddBtn').disabled=true;$('verifyAddBtn').textContent='Verifying…';msg.textContent='Connecting to Gmail SMTP…';try{const d=await api('/api/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,email,app_password:pwd})});if(!d.ok)throw new Error(d.msg||'Could not add account');closeAccountModal();toast('Gmail account verified and added');refreshOverview();refreshAccountsPage()}catch(e){msg.textContent=e.message}finally{accountRequestBusy=false;$('verifyAddBtn').disabled=false;$('verifyAddBtn').textContent='Verify & add'}}
function openNicheModal(encoded){const email=decodeURIComponent(encoded);nicheAccount=email;const a=(latestStats?.accounts||[]).find(x=>x.email===email);$('nicheAccountLabel').textContent=a?`${a.name||email} · ${email}`:email;$('allNiche').checked=!a?.niches?.length;$('accountDailyLimit').value=a?.daily_limit??5;$('accountWarmup').checked=!!a?.warmup_enabled;$('accountWarmupIncrement').value=a?.warmup_increment??0;$('accountWarmupMax').value=a?.warmup_max??(a?.daily_limit??5);$('nicheMsg').textContent='';$('nicheGrid').innerHTML=niches.map(([id,label])=>`<label class="niche-option"><input type="checkbox" value="${esc(id)}" ${a?.niches?.includes(id)?'checked':''}>${esc(label)}</label>`).join('');$('nicheModal').classList.add('open')}
function toggleAllNiches(checked){document.querySelectorAll('#nicheGrid input').forEach(x=>x.checked=false);if(checked)document.querySelectorAll('#nicheGrid input').forEach(x=>x.checked=false)}
function closeNicheModal(){nicheAccount=null;$('nicheModal').classList.remove('open')}
async function saveNicheSettings(){if(!nicheAccount)return;const a=(latestStats?.accounts||[]).find(x=>x.email===nicheAccount);const selected=$('allNiche').checked?[]:Array.from(document.querySelectorAll('#nicheGrid input:checked')).map(x=>x.value);const dailyLimit=Number($('accountDailyLimit').value),increment=Number($('accountWarmupIncrement').value),warmupMax=Number($('accountWarmupMax').value);if(!Number.isInteger(dailyLimit)||dailyLimit<1||!Number.isInteger(increment)||increment<0||!Number.isInteger(warmupMax)||warmupMax<dailyLimit){$('nicheMsg').textContent='Enter valid account limits. Warm-up maximum must be at least the daily limit.';return}try{await api('/api/account_settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:nicheAccount,active:a?.active!==false,niches:selected,daily_limit:dailyLimit,warmup_enabled:$('accountWarmup').checked,warmup_increment:increment,warmup_max:warmupMax})});closeNicheModal();toast('Account settings updated');refreshAccountsPage();refreshOverview()}catch(e){$('nicheMsg').textContent=e.message}}

async function loadSettings(){try{const d=await api('/api/settings');settingsData=d;d.fields.forEach(f=>{const el=$('set_'+f.key);if(!el)return;if(f.type==='bool')el.checked=f.value==='true';else el.value=f.value});updateDryUi();renderProviderEditor(d.multi_key_fields)}catch(e){toast(e.message)}}
function updateDryUi(){const dry=$('set_DRY_RUN').checked;$('dryTitle').textContent=dry?'Dry run':'Live outreach';$('drySub').textContent=dry?'No real emails are sent.':'Real emails can be delivered to leads.';const badge=$('modeBadge');badge.textContent=dry?'Dry run':'Live';badge.className='mode-pill '+(dry?'dry':'live')}
$('set_DRY_RUN').addEventListener('change',updateDryUi);
function renderProviderEditor(fields){const box=$('providerEditList');box.innerHTML='';fields.forEach(f=>{const card=document.createElement('div');card.className='provider-editor';card.innerHTML=`<div class="provider-editor-head"><strong style="font-size:11px">${esc(f.label)}</strong><span class="muted" style="font-size:9px">${f.count} configured</span></div><div class="key-list" data-field="${esc(f.key)}"></div><div class="new-key"><input type="password" placeholder="Paste a new key"><button class="btn" onclick="addKeyRow('${esc(f.key)}')">Add</button></div>`;box.appendChild(card);const list=card.querySelector('.key-list');for(let i=0;i<f.count;i++){const row=document.createElement('div');row.className='key-row';row.dataset.origIndex=i;row.innerHTML=`<span class="key-label">KEY ${i+1} · CONFIGURED</span><button class="btn" style="padding:6px 8px;font-size:9px" onclick="this.parentElement.remove()">Remove</button>`;list.appendChild(row)}})}
function addKeyRow(field){const list=document.querySelector(`.key-list[data-field="${CSS.escape(field)}"]`);const parent=list?.parentElement;if(!parent)return;const input=parent.querySelector('.new-key input');if(!input.value.trim()){input.focus();return}const row=document.createElement('div');row.className='key-row';row.dataset.new='1';row.innerHTML=`<span class="key-label">NEW KEY · ${esc(input.value.trim().slice(0,4))}••••</span><button class="btn" style="padding:6px 8px;font-size:9px" onclick="this.parentElement.remove()">Remove</button>`;list.appendChild(row);input.value=''}
async function saveAll(){const body={};document.querySelectorAll('#view-settings input[id^="set_"]').forEach(el=>body[el.id.replace('set_','')]=el.type==='checkbox'?el.checked:el.value);document.querySelectorAll('.key-list[data-field]').forEach(list=>{const field=list.dataset.field;const keep_indices=[],new_keys=[];list.querySelectorAll('.key-row').forEach(r=>{if(r.dataset.origIndex!==undefined)keep_indices.push(Number(r.dataset.origIndex))});const input=list.parentElement.querySelector('.new-key input');if(input&&input.value.trim())new_keys.push(input.value.trim());body[field]={keep_indices,new_keys}});$('settingsMsg').textContent='Saving…';try{const d=await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!d.ok)throw new Error(d.msg||'Could not save settings');$('settingsMsg').textContent='Saved.';toast('Settings saved');refreshOverview()}catch(e){$('settingsMsg').textContent=e.message}}

let sessionPollTimer=null, previousSessionActive=false, sessionInitialized=false;
function formatDuration(seconds){seconds=Math.max(0,Math.floor(Number(seconds)||0));const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60),s=seconds%60;return [h,m,s].map((v,i)=>i===0?String(v).padStart(2,'0'):String(v).padStart(2,'0')).join(':')}
function formatPerEmail(seconds,sent){if(!sent||seconds<=0)return '—';const avg=seconds/sent;return avg<10?avg.toFixed(1)+' sec/email':Math.round(avg)+' sec/email'}
function renderSession(d){const active=!!d.active,s=d.session||{};const elapsed=active?(s.elapsed_seconds||0):(s.duration_seconds||0),sent=s.emails_sent||0;$('sessionTimer').textContent=formatDuration(elapsed);$('sessionSent').textContent=sent;$('sessionRate').textContent=formatPerEmail(elapsed,sent);$('sessionNote').textContent=active?'Session is running. Average is elapsed session time divided by emails sent.':(d.history?.length?'Last session saved to history.':'No active session.')}
async function refreshSession(){try{const d=await api('/api/session');renderSession(d);if(sessionInitialized && previousSessionActive && !d.active && d.history?.length){const last=d.history[0];toast(`Session complete — ${formatDuration(last.duration_seconds)}, ${last.emails_sent} email${last.emails_sent===1?'':'s'} sent.`)}previousSessionActive=!!d.active;sessionInitialized=true;return d}catch(e){return null}}
function openSessionHistory(){api('/api/session/history').then(d=>{const box=$('sessionHistoryList');const rows=d.history||[];box.innerHTML=rows.length?rows.map(x=>`<div class="history-row"><div><div>${esc(new Date(x.started_at).toLocaleString())}</div><div class="history-muted">${esc(x.reason||'stopped')}</div></div><div><strong>${formatDuration(x.duration_seconds)}</strong><div class="history-muted">duration</div></div><div><strong>${esc(x.emails_sent)}</strong><div class="history-muted">emails</div></div><div><strong>${x.emails_sent&&x.duration_seconds?((x.duration_seconds/x.emails_sent)<10?(x.duration_seconds/x.emails_sent).toFixed(1):(Math.round(x.duration_seconds/x.emails_sent)))+' sec/email':'—'}</strong><div class="history-muted">avg time / email</div></div></div>`).join(''):'<div class="empty">No sessions recorded yet.</div>';$('sessionHistoryModal').classList.add('open')}).catch(e=>toast(e.message))}
function closeSessionHistory(){$('sessionHistoryModal').classList.remove('open')}
async function boot(){try{const r=await api('/api/account_settings');niches=r.niches||[];const hash=location.hash.slice(1);setView(['overview','accounts','settings'].includes(hash)?hash:'overview');await refreshOverview()}catch(e){$('statusText').textContent='Unavailable';toast(e.message)}}
boot();setInterval(()=>{if(currentView==='overview')refreshOverview();else if(currentView==='accounts')refreshAccountsPage()},7000);setInterval(()=>{if(currentView==='overview')refreshSession()},1000);setInterval(()=>{if(currentView==='overview')refreshLogs()},9000);
</script>
</body></html>"""

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    print("Outreach Agent dashboard running at http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
