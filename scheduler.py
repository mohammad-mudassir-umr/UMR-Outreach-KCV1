"""Asynchronous bounded-concurrency orchestration for UMR Outreach.

The scheduler is deliberately separate from the email-generation logic.  It
uses asyncio queues/workers for orchestration, while existing blocking HTTP and
SMTP functions are executed only at the I/O boundary via asyncio.to_thread().
This keeps the architecture event-driven without turning the entire agent into
a thread-per-job design.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

class AccountDispatcher:
    """Chooses the earliest eligible sender account while enforcing per-account pacing.

    Selection happens in the event loop, so no two send workers can reserve the
    same account at the same instant.  A reservation is released when the send
    finishes; successful sends increment the existing Warmup state.
    """

    def __init__(self, accounts: list[str], interval_seconds: int, strategy: str = "auto"):
        self.accounts = list(accounts)
        self.interval = max(0, int(interval_seconds))
        self.strategy = str(strategy or "auto").strip().lower()
        self._round_robin_index = 0
        self._lock = asyncio.Lock()
        self._next_available = {a: 0.0 for a in self.accounts}
        self._reserved = {a: 0 for a in self.accounts}

    async def choose(self, is_eligible: Callable[[str], bool], remaining: Callable[[str], int]) -> Optional[tuple[str, float]]:
        async with self._lock:
            now = time.monotonic()
            candidates = []
            for account in self.accounts:
                if self._reserved[account] >= remaining(account):
                    continue
                if remaining(account) <= 0:
                    continue
                if not is_eligible(account):
                    continue
                candidates.append((max(now, self._next_available[account]), account))
            if not candidates:
                return None
            if self.strategy in ("auto", "capacity_first"):
                # Prefer the account with the most remaining capacity while
                # preserving the earliest legal send time as the primary gate.
                ready_at, account = min(
                    candidates,
                    key=lambda x: (x[0], -int(remaining(x[1])))
                )
            elif self.strategy == "round_robin":
                ordered = sorted(candidates, key=lambda x: x[1])
                ready_at, account = ordered[self._round_robin_index % len(ordered)]
                self._round_robin_index = (self._round_robin_index + 1) % len(ordered)
            else:  # balanced
                ready_at, account = min(candidates)
            self._reserved[account] += 1
            self._next_available[account] = max(now, ready_at) + self.interval
            return account, max(0.0, ready_at - now)

    async def release(self, account: str) -> None:
        async with self._lock:
            self._reserved[account] = max(0, self._reserved.get(account, 0) - 1)
