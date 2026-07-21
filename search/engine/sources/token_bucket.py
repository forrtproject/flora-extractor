"""
Simple monotonic-clock rate limiter.

Each source adapter holds one bucket. Every API call calls ``take()``, which
sleeps just long enough since the last call to respect the current rate.
After a 429, callers can ``set_rate()`` lower defensively. Adapters call
these sequentially in a single while-loop (no threads), so no locking.
"""

import time


class TokenBucket:
    def __init__(self, rate_per_sec: float):
        if rate_per_sec <= 0:
            raise ValueError(f"TokenBucket: rate_per_sec must be > 0 (got {rate_per_sec})")
        self._rate = float(rate_per_sec)
        self._next_allowed = time.monotonic()

    def set_rate(self, rate_per_sec: float) -> None:
        if rate_per_sec > 0:
            self._rate = float(rate_per_sec)

    def get_rate(self) -> float:
        return self._rate

    def take(self) -> None:
        """Block until enough time has passed since the last call."""
        now = time.monotonic()
        if now < self._next_allowed:
            time.sleep(self._next_allowed - now)
            now = self._next_allowed
        self._next_allowed = now + 1.0 / self._rate
