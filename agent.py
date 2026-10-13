"""
Outreach Agent — agent.py

Orchestrates a bounded asynchronous pipeline:
load -> verify -> generate -> quality/fallback -> send queue -> SMTP.

The email-generation function itself is intentionally preserved. The speedup
comes from running that existing function through a bounded asyncio worker pool
and independently draining a send queue with per-account pacing/capacity.
Blocking HTTP/SMTP calls are isolated behind asyncio.to_thread(); asyncio
remains the orchestration layer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from datetime import datetime
from pathlib import Path

import account_settings
from ai_providers import ProviderManager
from config import Config
from emailer import Blacklist, SentLog, Warmup, send_email
from leads import CSVLeadSource, dedupe_against
from quality_gate import append_compliance_footer, validate_email
from replies import check_replies, twilio_client
from scheduler import AccountDispatcher
from templates import (
    assemble_email,
    deterministic_fallback,
    get_opener,
    load_template_library,
    match_template,
    pick_subject,
)
from verification import VerifaliaGate, verify_email
from observability import capture_exception, capture_message, flush_langfuse, init_sentry


def setup_logging(log_file: Path):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
    )


def generate_email_for_lead(cfg, providers, library, lead):
    """EXACT existing generation method; concurrency is added around it only."""
    template_id, tmpl = match_template(lead.niche, library)
    lead.template_id = template_id

    try:
        opener = get_opener(lead, tmpl, providers)
        subject = pick_subject(lead, tmpl)
        lead.subject_variant = subject

        body = assemble_email(lead, tmpl, opener, cfg.identity_name, cfg.brand_name)
        body = append_compliance_footer(body)

        ok, reasons = validate_email(subject, body)
        if not ok:
            logging.warning(
                "Quality gate rejected AI draft for %s (%s) — using deterministic fallback.",
                lead.email,
                "; ".join(reasons),
            )
            subject, body = deterministic_fallback(lead, tmpl, cfg.identity_name, cfg.brand_name)
            lead.subject_variant = subject
            body = append_compliance_footer(body)
    except Exception as e:
        logging.error("Generation failed for %s: %s — using deterministic fallback.", lead.email, e)
        subject, body = deterministic_fallback(lead, tmpl, cfg.identity_name, cfg.brand_name)
        lead.subject_variant = subject
        body = append_compliance_footer(body)

    return subject, body, template_id


async def _verify_leads(leads, verifalia, worker_count: int):
    """Run the existing verification logic concurrently, with bounded I/O."""
    queue = asyncio.Queue()
    results = []
    lock = asyncio.Lock()

    for lead in leads:
        await queue.put(lead)

    async def worker():
        while True:
            try:
                lead = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                ok = await asyncio.to_thread(verify_email, lead.email, verifalia)
                if ok:
                    async with lock:
                        results.append(lead)
            finally:
                queue.task_done()

    await asyncio.gather(*(worker() for _ in range(max(1, worker_count))))
    return results


async def run_batch_async(cfg: Config):
    providers = ProviderManager(cfg)
    library = load_template_library(cfg.templates_file)
    sent_log = SentLog(cfg.sent_log_csv)
    blacklist = Blacklist(cfg.blacklist_file)
    accounts = [u for u, _ in cfg.gmail_accounts]
    account_settings_snapshot = account_settings.load(cfg.state_dir)
    # Migrate the previous warm-up state into account-level controls without
    # changing existing account routing. New controls are only synthesized when
    # an account has no explicit daily-limit controls yet.
    legacy_warmup = {}
    raw_settings = {}
    settings_path = cfg.state_dir / "account_settings.json"
    if settings_path.exists():
        try:
            raw_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            raw_settings = {}
    if cfg.warmup_file.exists():
        try:
            legacy_warmup = json.loads(cfg.warmup_file.read_text(encoding="utf-8")).get("accounts", {})
        except Exception:
            legacy_warmup = {}
    changed_settings = False
    for a in accounts:
        raw = raw_settings.get(a, {}) if isinstance(raw_settings, dict) else {}
        if a not in raw_settings or "daily_limit" not in raw:
            old = legacy_warmup.get(a, {})
            prior_limit = max(1, int(old.get("daily_limit", cfg.default_account_daily_limit)))
            # Preserve the old gradual-ramp behavior during one-time migration.
            account_settings_snapshot[a] = {
                "active": account_settings_snapshot.get(a, {}).get("active", True),
                "niches": account_settings_snapshot.get(a, {}).get("niches", []),
                "daily_limit": prior_limit,
                "warmup_enabled": True,
                "warmup_increment": 3,
                "warmup_max": max(prior_limit, 100),
            }
            changed_settings = True
    if changed_settings:
        account_settings.save(cfg.state_dir, account_settings_snapshot)
    warmup = Warmup(
        cfg.warmup_file,
        accounts,
        cfg.default_account_daily_limit,
        0,
        cfg.global_daily_max,
        account_settings_snapshot,
    )
    verifalia = (
        VerifaliaGate(cfg.verifalia_api_key, cfg.verifalia_daily_cap, cfg.state_dir)
        if cfg.verifalia_api_key
        else None
    )
    twilio = twilio_client(cfg)

    # Reply checks remain a separate, lower-priority operation. They do not
    # consume the outbound generation/send queues.
    def on_reply(email, subject, body, intent, thread):
        if intent == "UNSUBSCRIBE":
            blacklist.add(email)
            thread["status"] = "handed_off"
        elif intent in ("INTERESTED", "UPSET"):
            thread["status"] = "handed_off"
            logging.info("%s reply from %s — forward manually or wire up auto-forward.", intent, email)

    await asyncio.to_thread(check_replies, cfg, sent_log, providers, twilio, on_reply)

    if not accounts:
        logging.warning("No Gmail accounts configured; nothing to send.")
        return

    active_at_start = [
        a for a in accounts if account_settings_snapshot.get(a, {}).get("active", True)
    ]
    logging.info("Configured Gmail senders: %s", accounts)
    logging.info("Active Gmail senders at batch start: %s", active_at_start)
    if not active_at_start:
        logging.warning("All Gmail accounts are paused; worker will exit without sending.")
        return

    # Warmup state is intentionally owned by the event-loop thread only.
    w = warmup.load()
    for a in accounts:
        w["accounts"].setdefault(
            a, {"sent_today": 0, "daily_limit": account_settings_snapshot.get(a, {}).get("daily_limit", cfg.default_account_daily_limit)}
        )

    source = CSVLeadSource(cfg.leads_csv)
    leads = source.load()
    leads = dedupe_against(leads, sent_log.already_sent(), blacklist.load())

    # Auto mode uses real remaining account capacity under the global safety
    # ceiling as the campaign work budget, avoiding unnecessary verification
    # and AI generation for work that cannot be sent today.
    active_capacity = sum(
        warmup.remaining(w, a)
        for a in accounts
        if account_settings_snapshot.get(a, {}).get("active", True)
    )
    sent_today_total = sum(int(v.get("sent_today", 0)) for v in w.get("accounts", {}).values())
    global_remaining = max(0, cfg.global_daily_max - sent_today_total)
    work_budget = min(len(leads), active_capacity, global_remaining)
    leads = leads[:work_budget]
    leads = await _verify_leads(leads, verifalia, max(2, min(12, cfg.ai_worker_count)))
    logging.info(
        "Loaded %d sendable leads after verification/dedupe/capacity planning (budget=%d).",
        len(leads), work_budget
    )

    (cfg.state_dir / "account_errors.json").write_text(
        json.dumps({"date": datetime.now().strftime("%Y-%m-%d"), "errors": {}}, indent=2),
        encoding="utf-8",
    )

    # Queues are bounded so generation cannot outrun the delivery layer forever.
    send_queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.queue_size)
    dispatcher = AccountDispatcher(accounts, cfg.send_interval_seconds, cfg.sending_strategy)
    account_cooldown: dict[str, float] = {}
    account_errors: dict[str, int] = {}
    skipped_no_eligible_account = 0
    sent_count = 0
    state_lock = asyncio.Lock()
    stop_event = asyncio.Event()
    generation_done = asyncio.Event()

    def signal_stop(*_):
        stop_event.set()
        logging.info("Stop requested — finishing in-flight operations and draining no new work.")

    installed_handlers = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(sig, signal_stop)
            installed_handlers.append(sig)
        except (NotImplementedError, RuntimeError):
            # Windows console delivery can raise KeyboardInterrupt instead.
            pass

    # The partition above would repeatedly scan the list. For large campaigns,
    # use indexed work queues instead. Keep the generator function untouched.
    generation_queue = asyncio.Queue()
    for lead in leads:
        await generation_queue.put(lead)

    async def generation_queue_worker(worker_no: int):
        # Keep draining queued items after a stop request so every queue
        # get() receives task_done() and generation_queue.join() cannot hang.
        while True:
            try:
                lead = generation_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if stop_event.is_set():
                    continue
                subject, body, template_id = await asyncio.to_thread(
                    generate_email_for_lead, cfg, providers, library, lead
                )
                # Bounded queue with stop-aware backpressure so a stop request
                # cannot strand a generation worker waiting forever.
                while not stop_event.is_set():
                    try:
                        await asyncio.wait_for(
                            send_queue.put((lead, subject, body, template_id)), timeout=0.5
                        )
                        break
                    except asyncio.TimeoutError:
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Generation worker %d failed for %s", worker_no + 1, lead.email)
            finally:
                generation_queue.task_done()

    def remaining(account: str) -> int:
        return warmup.remaining(w, account)

    def is_eligible(account: str, template_id: str, routing: dict) -> bool:
        now = time.monotonic()
        if account_cooldown.get(account, 0.0) > now:
            return False
        if not routing.get(account, {}).get("active", True):
            return False
        return account_settings.is_eligible(cfg.state_dir, account, template_id, routing)

    async def send_worker(worker_no: int):
        nonlocal skipped_no_eligible_account, sent_count
        while True:
            if generation_done.is_set() and send_queue.empty():
                return
            try:
                job = await asyncio.wait_for(send_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            lead, subject, body, template_id = job
            try:
                # Each lead gets at most one successful delivery. A failed
                # sender can be replaced by another eligible mailbox.
                sent_from = None
                attempted_accounts = set()
                max_attempts = max(1, len(accounts))

                for _ in range(max_attempts):
                    if stop_event.is_set():
                        break

                    routing = account_settings.load(cfg.state_dir)

                    def not_already_attempted(account: str) -> bool:
                        return account not in attempted_accounts and is_eligible(account, template_id, routing)

                    choice = await dispatcher.choose(not_already_attempted, remaining)
                    if choice is None:
                        # No immediately eligible sender. This can be temporary
                        # pacing or a dashboard pause; check once more before skip.
                        routing = account_settings.load(cfg.state_dir)
                        active = [a for a in accounts if is_eligible(a, template_id, routing) and remaining(a) > 0]
                        if not active:
                            break
                        await asyncio.sleep(0.25)
                        continue

                    account, delay = choice
                    attempted_accounts.add(account)
                    try:
                        if delay:
                            await asyncio.sleep(delay)
                        # Re-check after pacing delay so a dashboard pause takes
                        # effect before SMTP is called.
                        routing = account_settings.load(cfg.state_dir)
                        if not is_eligible(account, template_id, routing):
                            await dispatcher.release(account)
                            continue
                        sent_from = await asyncio.to_thread(
                            send_email, cfg, lead.email, subject, body, account
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logging.error(
                            "send_email raised for %s via %s: %s", lead.email, account, e
                        )
                        sent_from = None
                    finally:
                        await dispatcher.release(account)

                    if sent_from:
                        break

                    account_errors[account] = account_errors.get(account, 0) + 1
                    account_cooldown[account] = time.monotonic() + min(
                        60.0, 5.0 * account_errors[account]
                    )
                    logging.warning(
                        "Send failed via %s (%d failures this run) — trying another mailbox.",
                        account,
                        account_errors[account],
                    )

                if sent_from:
                    async with state_lock:
                        sent_log.record_send(
                            lead.name,
                            lead.email,
                            sent_from,
                            lead.niche,
                            lead.location,
                            template_id,
                            lead.subject_variant,
                        )
                        warmup.record_sent(w, sent_from)
                        warmup.save(w)
                        sent_count += 1
                    logging.info(
                        "Sent %d/%d today -> %s via %s (template=%s; worker=%d)",
                        w["accounts"][sent_from]["sent_today"],
                        w["accounts"][sent_from]["daily_limit"],
                        lead.email,
                        sent_from,
                        template_id,
                        worker_no + 1,
                    )
                else:
                    skipped_no_eligible_account += 1
                    logging.warning(
                        "Could not deliver %s — no eligible Gmail account available.", lead.email
                    )
            finally:
                send_queue.task_done()

    gen_workers = [
        asyncio.create_task(generation_queue_worker(i), name=f"ai-worker-{i+1}")
        for i in range(cfg.ai_worker_count)
    ]
    send_workers = [
        asyncio.create_task(send_worker(i), name=f"send-worker-{i+1}")
        for i in range(min(cfg.send_worker_count, max(1, len(accounts) * 2)))
    ]

    try:
        await generation_queue.join()
        await asyncio.gather(*gen_workers, return_exceptions=True)
        generation_done.set()
        await send_queue.join()
        if stop_event.is_set():
            logging.info("Stop requested; %d emails successfully sent before shutdown.", sent_count)
    finally:
        for task in gen_workers + send_workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*(gen_workers + send_workers), return_exceptions=True)

    (cfg.state_dir / "routing_skips.json").write_text(
        json.dumps(
            {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "skipped": skipped_no_eligible_account,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if account_errors:
        (cfg.state_dir / "account_errors.json").write_text(
            json.dumps(
                {"date": datetime.now().strftime("%Y-%m-%d"), "errors": account_errors},
                indent=2,
            ),
            encoding="utf-8",
        )
        logging.warning("Accounts with send failures this run: %s", account_errors)


def run_batch(cfg: Config):
    asyncio.run(run_batch_async(cfg))


def main():
    init_sentry()
    cfg = Config()
    setup_logging(cfg.log_file)
    cfg.validate()
    logging.info(
        "Outreach Agent starting | dry_run=%s | providers=%s | ai_workers=%d | send_workers=%d",
        cfg.dry_run,
        cfg.ai_provider_order,
        cfg.ai_worker_count,
        cfg.send_worker_count,
    )
    try:
        run_batch(cfg)
    except KeyboardInterrupt:
        logging.info("Outreach Agent interrupted by operator.")
    except Exception as exc:
        capture_exception(exc)
        capture_message("Outreach Agent terminated unexpectedly", level="error")
        raise
    finally:
        flush_langfuse()


if __name__ == "__main__":
    main()
