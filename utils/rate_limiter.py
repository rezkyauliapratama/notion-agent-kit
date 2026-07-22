"""Token bucket rate limiter."""

import asyncio
import time
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket rate limiter. Default: 3 tokens, 3/sec refill."""

    def __init__(self, max_rps: int = 3):
        self.capacity = max_rps
        self.refill_rate = max_rps
        self.tokens = float(max_rps)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            self._refill()
            while self.tokens < 1:
                wait_time = (1 - self.tokens) / self.refill_rate
                self._lock.release()
                try:
                    await asyncio.sleep(wait_time)
                finally:
                    await self._lock.acquire()
                self._refill()
            self.tokens -= 1

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    @property
    def available_tokens(self) -> float:
        self._refill()
        return self.tokens
