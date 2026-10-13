"""
Outreach Agent — config.py
Central place for environment variables and project-relative paths.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from accounts import list_accounts

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _csv_env(name: str, default: str = "") -> list:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _resolve_path(value: str, default: str) -> Path:
    p = Path(value or default)
    return p if p.is_absolute() else BASE_DIR / p


class Config:
    def __init__(self):
        # ── Core ──────────────────────────────────────────────────────────
        self.dry_run       = os.getenv("DRY_RUN", "true").strip().lower() == "true"
        self.identity_name = os.getenv("IDENTITY_NAME", "Adam").strip() or "Adam"
        self.brand_name    = os.getenv("BRAND_NAME", "Agency").strip()
        self.forward_email = os.getenv("FORWARD_EMAIL", "").strip()

        # ── AI provider cascade ──────────────────────────────────────────
        self.ai_provider_order = _csv_env("AI_PROVIDERS", "groq,openrouter,agnes,poixe")
        self.groq_keys         = _csv_env("GROQ_API_KEYS")
        self.openrouter_keys   = _csv_env("OPENROUTER_API_KEYS")
        self.agnes_keys        = _csv_env("AGNES_API_KEYS")
        self.poixe_keys        = _csv_env("POIXE_API_KEYS")
        self.groq_model        = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        self.openrouter_model  = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free").strip()
        self.agnes_model       = os.getenv("AGNES_MODEL", "agnes-2.5-flash").strip()
        self.poixe_model       = os.getenv("POIXE_MODEL", "deepseek-chat:free").strip()
        self.ai_worker_count   = max(1, int(os.getenv("AI_WORKER_COUNT", "8")))
        self.ai_provider_concurrency = max(1, int(os.getenv("AI_PROVIDER_CONCURRENCY", "2")))
        self.send_worker_count = max(1, int(os.getenv("SEND_WORKER_COUNT", "8")))
        self.queue_size        = max(10, int(os.getenv("PIPELINE_QUEUE_SIZE", "32")))

        # ── Project paths ────────────────────────────────────────────────
        self.state_dir       = _resolve_path(os.getenv("STATE_DIR", "state"), "state")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.leads_csv       = _resolve_path(os.getenv("LEADS_CSV", "data/leads.csv"), "data/leads.csv")
        self.sent_log_csv    = _resolve_path(os.getenv("SENT_LOG_CSV", "data/sent_log.csv"), "data/sent_log.csv")
        self.quarantine_csv  = _resolve_path(os.getenv("QUARANTINE_CSV", "data/quarantine.csv"), "data/quarantine.csv")
        self.templates_file  = _resolve_path(os.getenv("TEMPLATES_FILE", "templates/templates.json"), "templates/templates.json")

        # ── Gmail sending ────────────────────────────────────────────────
        # The dashboard-managed account store is the ONLY runtime source for
        # sender credentials. Legacy GMAIL_* env variables are intentionally
        # ignored so old accounts cannot silently reappear.
        managed_accounts = list_accounts(self.state_dir)
        self.gmail_accounts = [(a["email"], a["app_password"]) for a in managed_accounts]
        self.gmail_personas_by_email = {a["email"]: a["name"] for a in managed_accounts if a.get("email")}
        self.gmail_personas = [a["name"] for a in managed_accounts] or [self.identity_name]

        # ── Email verification ───────────────────────────────────────────
        self.verifalia_api_key   = os.getenv("VERIFALIA_API_KEY", "").strip()
        self.verifalia_daily_cap = int(os.getenv("VERIFALIA_DAILY_LIMIT", "25"))

        # ── Twilio ───────────────────────────────────────────────────────
        self.twilio_sid   = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        self.twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        self.twilio_from  = os.getenv("TWILIO_FROM_NUMBER", "").strip()
        self.twilio_to    = os.getenv("TWILIO_ALERT_NUMBER", "").strip()

        # ── Sending behavior / warmup ─────────────────────────────────────
        # Global safety ceiling. Individual account limits live in
        # state/account_settings.json.
        self.global_daily_max         = max(1, int(os.getenv("GLOBAL_DAILY_MAX", "1000")))
        # One authoritative conservative pacing rule for actual SMTP sends.
        # It is intentionally not exposed as a duplicate dashboard setting.
        self.send_interval_seconds    = max(0, int(os.getenv("SEND_INTERVAL_SECONDS", "60")))
        self.default_account_daily_limit = max(1, int(os.getenv("DEFAULT_ACCOUNT_DAILY_LIMIT", "5")))
        self.sending_strategy = os.getenv("SENDING_STRATEGY", "auto").strip().lower()
        self.followup_after_days     = int(os.getenv("FOLLOWUP_AFTER_DAYS", "3"))
        self.max_followups           = int(os.getenv("MAX_FOLLOWUPS", "1"))
        self.max_thread_exchanges    = int(os.getenv("MAX_THREAD_EXCHANGES", "2"))
        self.skip_outside_business_hours = os.getenv(
            "SKIP_OUTSIDE_BUSINESS_HOURS", "true"
        ).strip().lower() == "true"

        self.warmup_file     = self.state_dir / "warmup_state.json"
        self.threads_file    = self.state_dir / "threads.json"
        self.blacklist_file  = self.state_dir / "blacklist.txt"
        self.log_file        = self.state_dir / "agent.log"

    def validate(self):
        missing = []
        if not (self.groq_keys or self.openrouter_keys or self.agnes_keys or self.poixe_keys):
            missing.append("at least one configured AI provider API key")
        if not self.gmail_accounts:
            missing.append("at least one Gmail account in the dashboard")
        if not self.forward_email:
            missing.append("FORWARD_EMAIL")
        if not self.leads_csv.exists():
            missing.append(f"leads file at {self.leads_csv} (upload one or point LEADS_CSV at it)")
        if missing:
            raise RuntimeError(f"Missing required config: {', '.join(missing)}")
