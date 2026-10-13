import json
import os
import tempfile
import unittest
import asyncio
from pathlib import Path

import account_settings
import accounts
from emailer import Warmup
from templates import pick_subject
from scheduler import AccountDispatcher
from ai_providers import ProviderManager
from types import SimpleNamespace
from unittest.mock import patch


class RegressionTests(unittest.TestCase):
    def test_account_store_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            a = accounts.add(state, "Test Sender", "tester@gmail.com", "abcdefghijklmnop")
            self.assertEqual(accounts.get(state, a["id"])["email"], "tester@gmail.com")
            self.assertNotIn("app_password", accounts.public(a))

    def test_paused_account_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            accounts.add(state, "Paused", "paused@gmail.com", "abcdefghijklmnop")
            account_settings.save(state, {"paused@gmail.com": {"active": False, "niches": []}})
            self.assertFalse(account_settings.is_eligible(state, "paused@gmail.com", "restaurants"))

    def test_new_missing_settings_default_active_only_for_new_account(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            account_settings.save(state, {"paused@gmail.com": {"active": False, "niches": []}})
            self.assertFalse(account_settings.get(state, "paused@gmail.com")["active"])
            self.assertTrue(account_settings.get(state, "new@gmail.com")["active"])

    def test_warmup_roundtrip_and_new_account(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "warmup.json"
            w = Warmup(state, ["a@gmail.com"], 5, 3, 100)
            data = w.load()
            self.assertEqual(w.remaining(data, "a@gmail.com"), 5)
            w.record_sent(data, "a@gmail.com")
            w.save(data)
            again = w.load()
            self.assertEqual(again["accounts"]["a@gmail.com"]["sent_today"], 1)



    def test_provider_requests_rotate_across_configured_families(self):
        cfg = SimpleNamespace(
            groq_keys=["g"], openrouter_keys=["o"], agnes_keys=["a"], poixe_keys=["p"],
            groq_model="g", openrouter_model="o", agnes_model="a", poixe_model="p",
            ai_provider_order=["groq", "openrouter", "agnes", "poixe"],
            ai_provider_concurrency=2, state_dir=Path(tempfile.mkdtemp()),
        )
        manager = ProviderManager(cfg)
        calls = []
        def fake_try(provider, prompt):
            calls.append(provider)
            return "ok"
        with patch.object(manager, "_try_provider", side_effect=fake_try):
            [manager.generate("x") for _ in range(4)]
        self.assertEqual(calls, ["groq", "openrouter", "agnes", "poixe"])


    def test_removed_legacy_providers_and_current_provider_defaults(self):
        cfg = SimpleNamespace(
            groq_keys=[], openrouter_keys=[], agnes_keys=[], poixe_keys=[],
            groq_model="g", openrouter_model="minimax/minimax-m3:free",
            agnes_model="agnes-2.5-flash", poixe_model="deepseek-chat:free",
            ai_provider_order=["groq", "openrouter", "agnes", "poixe"],
            ai_provider_concurrency=1, state_dir=Path(tempfile.mkdtemp()),
        )
        manager = ProviderManager(cfg)
        self.assertNotIn("gemini", manager._key_pools)
        self.assertNotIn("cerebras", manager._key_pools)
        self.assertNotIn("sambanova", manager._key_pools)
        self.assertEqual(manager._models["agnes"], "agnes-2.5-flash")
        self.assertEqual(manager._models["poixe"], "deepseek-chat:free")

    def test_agnes_transient_500_is_retried(self):
        cfg = SimpleNamespace(
            groq_keys=[], openrouter_keys=[], agnes_keys=["a"], poixe_keys=[],
            groq_model="g", openrouter_model="o", agnes_model="agnes-2.5-flash", poixe_model="p",
            ai_provider_order=["agnes"], ai_provider_concurrency=1, state_dir=Path(tempfile.mkdtemp()),
        )
        manager = ProviderManager(cfg)
        class Resp:
            def __init__(self, code): self.status_code=code
            def raise_for_status(self):
                if self.status_code >= 400: raise RuntimeError("http")
            def json(self): return {"choices":[{"message":{"content":"ok"}}]}
        with patch("ai_providers.requests.post", side_effect=[Resp(500), Resp(200)]), patch("ai_providers.time.sleep"):
            self.assertEqual(manager._call_agnes("a", "hello"), "ok")

    def test_transient_5xx_is_retried(self):
        cfg = SimpleNamespace(
            groq_keys=["g"], openrouter_keys=[], agnes_keys=[], poixe_keys=[],
            groq_model="g", openrouter_model="o", agnes_model="a", poixe_model="p",
            ai_provider_order=["groq"], ai_provider_concurrency=1, state_dir=Path(tempfile.mkdtemp()),
        )
        manager = ProviderManager(cfg)
        responses = [500, 200]
        class Resp:
            def __init__(self, code): self.status_code=code
            def raise_for_status(self):
                if self.status_code >= 400: raise RuntimeError("http")
            def json(self): return {"choices":[{"message":{"content":"ok"}}]}
        with patch("ai_providers.requests.post", side_effect=[Resp(500), Resp(200)]), patch("ai_providers.time.sleep"):
            self.assertEqual(manager._call_groq("g", "hello"), "ok")


    def test_account_dispatcher_never_over_reserves_capacity(self):
        async def run():
            d = AccountDispatcher(["a@gmail.com"], 0)
            first = await d.choose(lambda a: True, lambda a: 1)
            second = await d.choose(lambda a: True, lambda a: 1)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            await d.release("a@gmail.com")
            third = await d.choose(lambda a: True, lambda a: 1)
            self.assertIsNotNone(third)
            await d.release("a@gmail.com")
        asyncio.run(run())

    def test_account_level_limits_and_warmup(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            account_settings.save(state, {"a@gmail.com": {
                "active": True, "niches": [], "daily_limit": 7,
                "warmup_enabled": True, "warmup_increment": 3, "warmup_max": 20
            }})
            settings = account_settings.load(state)
            w = Warmup(state / "warmup.json", ["a@gmail.com"], 5, 0, 100, settings)
            data = w.load()
            self.assertEqual(data["accounts"]["a@gmail.com"]["daily_limit"], 7)
            w.record_sent(data, "a@gmail.com")
            w.save(data)
            raw = json.loads((state / "warmup.json").read_text())
            raw["date"] = "2000-01-01"
            (state / "warmup.json").write_text(json.dumps(raw))
            next_day = w.load()
            self.assertEqual(next_day["accounts"]["a@gmail.com"]["daily_limit"], 10)

    def test_auto_dispatcher_uses_all_accounts_before_reusing_one(self):
        async def run():
            d = AccountDispatcher(["a", "b", "c"], 60, "auto")
            remaining = {"a": 10, "b": 10, "c": 10}
            picked = []
            for _ in range(3):
                choice = await d.choose(lambda _: True, lambda a: remaining[a])
                self.assertIsNotNone(choice)
                picked.append(choice[0])
                remaining[choice[0]] -= 1
            self.assertEqual(set(picked), {"a", "b", "c"})
        asyncio.run(run())

    def test_subject_pick_is_stable(self):
        class Lead: email = "person@example.com"; name = "Acme"; location = "Dhaka"
        tmpl = {"subject_options": ["quick thought for {{company}}", "hello {{company}}", "{{company}} idea"]}
        self.assertEqual(pick_subject(Lead(), tmpl), pick_subject(Lead(), tmpl))

    def test_session_duration_and_sent_count_helpers(self):
        from session_history import session_sent_count
        with tempfile.TemporaryDirectory() as td:
            sent_rows = [{"Date Sent": "2026-01-01 12:00:05", "Email": "a@example.com"}, {"Date Sent": "2026-01-01 12:01:00", "Email": "b@example.com"}]
            self.assertEqual(session_sent_count(sent_rows, "2026-01-01T12:00:00+00:00", "2026-01-01T12:00:30+00:00"), 1)


if __name__ == "__main__":
    unittest.main()
