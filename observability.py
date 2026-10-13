"""Optional observability integrations for UMR Outreach.

Observability must never be allowed to break the outreach pipeline. Credentials
come only from environment variables; sensitive lead/email content is not sent
to third-party tracing services.
"""
from __future__ import annotations

import hashlib
import logging
import os
from contextlib import nullcontext

try:
    import sentry_sdk
except Exception:
    sentry_sdk = None

try:
    from langfuse import get_client
except Exception:
    get_client = None


def init_sentry() -> bool:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn or sentry_sdk is None:
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENVIRONMENT", "local"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            send_default_pii=False,
        )
        return True
    except Exception:
        logging.exception("Sentry initialization failed; continuing without telemetry.")
        return False


def capture_exception(exc: BaseException) -> None:
    if sentry_sdk is None or not os.getenv("SENTRY_DSN", "").strip():
        return
    try:
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def capture_message(message: str, level: str = "info") -> None:
    if sentry_sdk is None or not os.getenv("SENTRY_DSN", "").strip():
        return
    try:
        sentry_sdk.capture_message(message, level=level)
    except Exception:
        pass


def lead_fingerprint(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def langfuse_client():
    if get_client is None:
        return None
    if not os.getenv("LANGFUSE_PUBLIC_KEY", "").strip() or not os.getenv("LANGFUSE_SECRET_KEY", "").strip():
        return None
    try:
        return get_client()
    except Exception:
        logging.exception("Langfuse initialization failed; continuing without telemetry.")
        return None


def generation_observation(provider: str, model: str, prompt_fingerprint: str):
    client = langfuse_client()
    if client is None:
        return nullcontext(None)
    return client.start_as_current_observation(
        as_type="generation",
        name="email-generation",
        model=model or None,
        metadata={
            "provider": provider,
            "prompt_fingerprint": prompt_fingerprint,
        },
    )


def flush_langfuse() -> None:
    client = langfuse_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        pass
