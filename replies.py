"""
Outreach Agent — replies.py
IMAP reply checking, restricted to senders who are actually in sent_log.csv
(the bug we fixed in KCv1 — system/notification emails must never get
classified as prospect replies or trigger a hot-lead SMS).
"""
import imaplib
import json
import logging
import re
import os
import shutil
import tempfile
from email import message_from_bytes
from pathlib import Path
from typing import Dict, Optional

try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False


class ThreadStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self, data: Dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix="threads.", suffix=".tmp")
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


def twilio_client(cfg) -> Optional["TwilioClient"]:
    if not (TWILIO_AVAILABLE and cfg.twilio_sid and cfg.twilio_token
            and cfg.twilio_from and cfg.twilio_to):
        return None
    return TwilioClient(cfg.twilio_sid, cfg.twilio_token)


def send_sms_alert(cfg, twilio, message: str):
    if not twilio:
        return
    try:
        twilio.messages.create(body=f"[Outreach Agent] {message}", from_=cfg.twilio_from, to=cfg.twilio_to)
    except Exception as e:
        logging.warning("SMS alert failed (non-critical): %s", e)


def classify_intent(provider_manager, body: str) -> str:
    prompt = (
        "Classify this email reply into exactly one label:\n"
        "INFO, PRICING, INTERESTED, UNSUBSCRIBE, UPSET, OTHER\n\n"
        f"Reply:\n{body}\n\nRespond with only the label word."
    )
    try:
        intent = provider_manager.generate(prompt).upper().strip()
    except Exception:
        return "OTHER"
    for label in ("INFO", "PRICING", "INTERESTED", "UNSUBSCRIBE", "UPSET", "OTHER"):
        if label in intent:
            return label
    return "OTHER"


def known_sent_emails(sent_log) -> set:
    """Only ever process replies from people we actually emailed."""
    return sent_log.already_sent()


def check_replies(cfg, sent_log, provider_manager, twilio, on_reply_callback=None):
    """
    on_reply_callback(email, subject, body, intent) is invoked for anything
    classified as INTERESTED/UPSET/UNSUBSCRIBE so the caller (agent.py) can
    forward it, update state, etc. Keeping this decoupled from send_email
    avoids a circular import.
    """
    known = known_sent_emails(sent_log)
    threads = ThreadStore(cfg.threads_file)
    thread_data = threads.load()

    for user, pwd in cfg.gmail_accounts:
        if cfg.dry_run:
            logging.info("[DRY RUN] Skipping IMAP for %s", user)
            continue
        mail = None
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(user, pwd)
            mail.select("inbox")
            _, resp = mail.search(None, "UNSEEN")
            for num in (resp[0] or b"").split():
                _, data = mail.fetch(num, "(RFC822)")
                msg = message_from_bytes(data[0][1])
                sender = msg.get("From", "")
                subject = msg.get("Subject", "")

                match = re.search(r"[\w.\-]+@[\w.\-]+\.\w+", sender or "")
                sender_addr = (match.group(0) if match else sender).strip().lower()
                if sender_addr not in known:
                    mail.store(num, "+FLAGS", "\\Seen")
                    continue

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if (part.get_content_type() == "text/plain"
                                and "attachment" not in str(part.get("Content-Disposition"))):
                            payload = part.get_payload(decode=True)
                            if payload:
                                body += payload.decode("utf-8", errors="ignore")
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="ignore")

                thread = thread_data.setdefault(sender_addr, {"exchanges": 0, "status": "active"})
                if thread.get("status") != "handed_off":
                    thread["exchanges"] = int(thread.get("exchanges", 0)) + 1
                    intent = classify_intent(provider_manager, body)
                    if intent in ("INTERESTED", "UPSET"):
                        send_sms_alert(cfg, twilio, f"{intent} reply from {sender_addr} | {subject[:60]}")
                    if on_reply_callback:
                        on_reply_callback(sender_addr, subject, body, intent, thread)

                mail.store(num, "+FLAGS", "\\Seen")
        except Exception as e:
            logging.error("Reply check failed for %s: %s", user, e)
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass

    threads.save(thread_data)
