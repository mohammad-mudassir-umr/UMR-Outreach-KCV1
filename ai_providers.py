"""Concurrent-capable AI provider pool.

The existing prompt/generation behavior is unchanged.  This module only adds
more provider slots, bounded per-provider concurrency, cooldowns, and thread-safe
resource selection around the existing blocking HTTP clients.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Dict, Optional

import requests

from observability import generation_observation, lead_fingerprint


class ProviderExhausted(Exception):
    pass


class ProviderManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self._idx: Dict[str, int] = {p: 0 for p in ("groq", "openrouter", "agnes", "poixe")}
        self._provider_idx = 0
        self._provider_order_lock = threading.Lock()
        self._key_pools = {
            "groq": cfg.groq_keys,
            "openrouter": cfg.openrouter_keys,
            "agnes": cfg.agnes_keys,
            "poixe": cfg.poixe_keys,
        }
        self._models = {
            "groq": cfg.groq_model,
            "openrouter": cfg.openrouter_model,
            "agnes": cfg.agnes_model,
            "poixe": cfg.poixe_model,
        }
        self._locks = {p: threading.Lock() for p in self._key_pools}
        self._semaphores = {
            p: threading.BoundedSemaphore(max(1, cfg.ai_provider_concurrency))
            for p in self._key_pools
        }
        self._cooldowns: Dict[tuple[str, int], float] = {}
        self._health_lock = threading.Lock()

    def _reserve_key(self, provider: str) -> tuple[int, str]:
        keys = self._key_pools.get(provider) or []
        if not keys:
            raise ProviderExhausted(f"{provider} is not configured")
        with self._locks[provider]:
            now = time.monotonic()
            start = self._idx[provider]
            for offset in range(len(keys)):
                i = (start + offset) % len(keys)
                if self._cooldowns.get((provider, i), 0.0) <= now:
                    self._idx[provider] = (i + 1) % len(keys)
                    return i, keys[i]
        raise ProviderExhausted(f"{provider} temporarily cooling down")

    def _cooldown(self, provider: str, index: int, seconds: float) -> None:
        with self._locks[provider]:
            self._cooldowns[(provider, index)] = max(
                self._cooldowns.get((provider, index), 0.0), time.monotonic() + seconds
            )

    @staticmethod
    def _extract_openai_text(data: dict, provider: str) -> str:
        text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise RuntimeError(f"{provider} returned empty content")
        return text

    def _post_openai_compatible(self, provider: str, key: str, prompt: str, base_url: str) -> str:
        model = self._models[provider]
        if not model:
            raise RuntimeError(f"{provider} has no model configured")
        import hashlib
        prompt_fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        with generation_observation(provider, model, prompt_fingerprint) as observation:
            last_exc = None
            # Provider gateways can return transient 5xx errors. Retry those
            # with bounded exponential backoff; do not retry 4xx configuration
            # or authentication failures. This preserves the existing prompt
            # and generation behavior while improving provider success rate.
            for attempt in range(3):
                try:
                    r = requests.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.4},
                        timeout=45,
                    )
                    if r.status_code == 429:
                        raise ProviderExhausted(f"{provider} rate-limited")
                    if r.status_code in (500, 502, 503, 504, 520):
                        last_exc = RuntimeError(f"{provider} transient HTTP {r.status_code}")
                        if attempt < 2:
                            time.sleep(1.0 * (2 ** attempt))
                            continue
                        raise last_exc
                    r.raise_for_status()
                    text = self._extract_openai_text(r.json(), provider)
                    if observation is not None:
                        observation.update(output={"chars": len(text), "provider": provider})
                    return text
                except (requests.Timeout, requests.ConnectionError) as exc:
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(1.0 * (2 ** attempt))
                        continue
                    raise
            raise last_exc or RuntimeError(f"{provider} request failed")

    def _call_groq(self, key: str, prompt: str) -> str:
        return self._post_openai_compatible("groq", key, prompt, "https://api.groq.com/openai/v1")

    def _call_openrouter(self, key: str, prompt: str) -> str:
        return self._post_openai_compatible("openrouter", key, prompt, "https://openrouter.ai/api/v1")

    def _call_agnes(self, key: str, prompt: str) -> str:
        return self._post_openai_compatible("agnes", key, prompt, "https://apihub.agnes-ai.com/v1")

    def _call_poixe(self, key: str, prompt: str) -> str:
        return self._post_openai_compatible("poixe", key, prompt, "https://api.poixe.com/v1")

    _CALLERS = {
        "groq": "_call_groq", "openrouter": "_call_openrouter",
        "agnes": "_call_agnes", "poixe": "_call_poixe",
    }

    def _try_provider(self, provider: str, prompt: str) -> Optional[str]:
        keys = self._key_pools.get(provider) or []
        if not keys:
            return None
        caller = getattr(self, self._CALLERS[provider])
        with self._semaphores[provider]:
            try:
                index, key = self._reserve_key(provider)
            except ProviderExhausted:
                return None
            try:
                text = caller(key, prompt)
                self._record_health(provider, up=True)
                return text
            except ProviderExhausted as e:
                self._cooldown(provider, index, 30.0)
                logging.warning("%s key #%d rate-limited/exhausted: %s", provider, index, e)
                self._record_health(provider, up=False)
                return None
            except Exception as e:
                self._cooldown(provider, index, 5.0)
                logging.warning("%s key #%d failed: %s", provider, index, e)
                return None

    def _record_health(self, provider: str, up: bool):
        path = self.cfg.state_dir / "provider_health.json"
        with self._health_lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception:
                data = {}
            data[provider] = {
                "status": "up" if up else "down",
                "last_checked": time.strftime("%Y-%m-%d %H:%M:%S"),
                "key_count": len(self._key_pools.get(provider) or []),
            }
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass

    def _ordered_providers_for_request(self) -> list[str]:
        providers = [p.strip().lower() for p in self.cfg.ai_provider_order if p.strip().lower() in self._CALLERS]
        providers = [p for p in providers if self._key_pools.get(p)]
        if not providers:
            return []
        # Round-robin across provider families so healthy providers actually
        # work concurrently instead of the first provider monopolizing every
        # generation until it hits a rate limit. Fallback order remains the
        # configured order after the selected starting provider.
        with self._provider_order_lock:
            start = self._provider_idx % len(providers)
            self._provider_idx = (self._provider_idx + 1) % len(providers)
        return providers[start:] + providers[:start]

    def generate(self, prompt: str) -> str:
        errors = []
        for provider in self._ordered_providers_for_request():
            result = self._try_provider(provider, prompt)
            if result:
                return result
            errors.append(provider)
        raise RuntimeError(f"All AI providers failed or unconfigured: {errors}")

    def generate_json(self, prompt: str, retries: int = 1) -> dict:
        last_err = None
        for _ in range(retries + 1):
            raw = self.generate(prompt)
            cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if match:
                cleaned = match.group(0)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as e:
                last_err = e
                prompt = prompt + "\n\nReturn ONLY valid JSON. No prose, no code fences."
                time.sleep(0.2)
        raise RuntimeError(f"Model did not return valid JSON after {retries + 1} attempts: {last_err}")
