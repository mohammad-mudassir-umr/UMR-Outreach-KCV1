# Outreach Agent

A dedicated cold-email agent, scoped down from KCv1: no lead-finding or web
scraping — leads come from a CSV you provide. Its only job is crafting and
sending emails, checking replies, and alerting you on hot leads.

## Architecture

```
data/leads.csv  ──▶  leads.py (CSVLeadSource)
                          │
                          ▼
              verification.py (MX check, always free/unlimited;
                                 optional capped Verifalia)
                          │
                          ▼
              templates.py — deterministic template match by niche
                          │
                          ▼
     ai_providers.py — CoT pipeline via provider cascade
     (Groq → OpenRouter → Agnes → Poixe, rotating key pools)
       1. reasoning        (scratch context, never sent)
       2. slot-fill JSON   (only HOOK / PAIN_ACK / VALUE_PROP — everything
                             else in the template is fixed, deterministic text)
       3. subject lines    (3 candidates, scored + picked in CODE, not by the AI)
                          │
                          ▼
              quality_gate.py — leaked placeholders, length, spam words,
                                  compliance footer. Fails → deterministic_fallback()
                                  (zero-AI, plain substitution) instead of retry-and-hope.
                          │
                          ▼
              emailer.py — SMTP + per-account app passwords, warmup ramp (not a flat cap),
                             atomic sent_log.csv writes, blacklist
                          │
                          ▼
              replies.py — IMAP, restricted to senders already in sent_log.csv
                             (prevents system/notification emails from being
                             misread as prospect replies), Twilio hot-lead SMS
                          │
                          ▼
              dashboard.py — Flask: upload leads, Gmail account manager, live stats, start/stop, logs
```

## Why no lead-finding

KCv1 also searched for leads (Serper/Google/Tavily), scraped websites, and
scored them — that's three extra paid/rate-limited APIs and a lot of
complexity for something a spreadsheet already solves. This agent assumes
you already have your leads; it focuses entirely on making the email good
and getting it delivered safely.

## Why the prompt architecture looks like this

Freeform "write a custom email, don't change the structure" instructions get
ignored by small free models. Instead, each template defines fixed text
(greeting, CTA, signature, footer) plus a small number of named slots
(`HOOK`, `PAIN_ACK`, `VALUE_PROP`). The model's only output is JSON for those
slots — it has no surface area to rewrite the structure because the
structure was never in its output. Subject-line "best pick" is scored in
code (length, spam words, personalization, no all-caps) rather than asking
the model to judge its own output, since small models are unreliable
self-critics.

`templates/templates.json` is seeded from your original 8-industry brief
(hook angle / pain stat / solution / CTA per niche) so this isn't blocked on
having your proven drafts in hand yet — swap in real copy once you have
reply-rate data telling you what's actually working.

## On "multiple API keys to beat rate limits"

Groq's free-tier limits are enforced per **organization** /
**project**, not per key — extra keys on the same account don't add
capacity, and both providers' terms prohibit creating multiple accounts to
get around limits. At 30-100 sends/day (roughly 200-400 AI calls/day across
reasoning + slot-fill + subject generation + reply classification), a single
Groq account already covers you comfortably. The multi-provider cascade here
is for **resilience** — provider quotas can change without warning, so the
one update — not for stacking capacity. Treat `GROQ_API_KEYS` etc. as
comma-separated pools for when you *do* have multiple legitimate accounts,
not as a rate-limit workaround.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
# put your leads in data/leads.csv (see data/leads_template.csv for the schema)
python dashboard.py    # http://localhost:5050 — upload leads, Start
# or run the agent directly:
python agent.py
```

### Gmail accounts

Gmail senders are managed from the **Accounts** page instead of `.env`. Add each sender with:

- a display name (used as the sender name)
- the Gmail address
- the Gmail app password

The dashboard verifies the credentials against Gmail SMTP before saving the account. Accounts are stored locally in `state/accounts.json`; app passwords are never sent back to the browser. Account changes are locked while the agent is running. The interface is designed for 20+ accounts and supports search, per-account activation, niche routing, send limits, reply rates, and removal.

Required lead CSV columns: `Name`, `Email`, `Niche`.
Recommended: `Location`, `Pain Point`, `Website`, `Trigger`, `Notes`.

Leave `DRY_RUN=true` until you've read through a batch of generated emails
in `state/agent.log` and you're happy with the quality — there's no
in-dashboard approval queue by design, so this read-through is the one
manual gate before real sends.

## What's not built yet

- **Google Sheets as a live lead source** — `leads.py` has `LeadSource` as an
  interface specifically so this is a single new class, not a rewrite.
- **Follow-up sequencing** — KCv1's follow-up logic (re-contact after N days
  with fresh angle) isn't ported yet; `MAX_FOLLOWUPS`/`FOLLOWUP_AFTER_DAYS`
  are in config as placeholders for it.
- **Verifalia's actual submit/poll flow** — `verification.py` has the shape
  of the integration and the daily-usage cap, but the real API call needs
  testing against a live Verifalia account.
- **Reply auto-forwarding** — `agent.py`'s `on_reply` callback logs
  INTERESTED/UPSET/UNSUBSCRIBE but doesn't forward the email to you yet
  (KCv1's `forward()` pattern can be ported in directly).
