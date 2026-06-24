"""
Simple monotonic-clock rate limiter.

Each source adapter holds one bucket. Every API call calls ``take()``,
which sleeps just long enough for a token to be available. After a 429,
callers can ``set_rate()`` lower defensively.
"""

import time
from threading import Lock


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int):
        if rate_per_sec <= 0:
            raise ValueError(f"TokenBucket: rate_per_sec must be > 0 (got {rate_per_sec})")
        if burst <= 0:
            raise ValueError(f"TokenBucket: burst must be > 0 (got {burst})")
        self._rate = float(rate_per_sec)
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = Lock()

    def set_rate(self, rate_per_sec: float) -> None:
        if rate_per_sec <= 0:
            return
        with self._lock:
            self._rate = float(rate_per_sec)

    def get_rate(self) -> float:
        return self._rate

    def take(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = max(0.001, (1.0 - self._tokens) / self._rate)
            time.sleep(min(wait, 1.0))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now
