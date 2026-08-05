"""
rate_limit.py — one call queue per remote service, shared by every thread.

Read-then-sleep is not a rate limit once more than one thread calls the same
service: N threads read the same "last call" timestamp, sleep the same remainder
and then fire together, which divides the configured interval by N — exactly the
burst the service's limit exists to prevent. `throttle()` instead RESERVES the
next free instant under a lock and sleeps until that instant, so N concurrent
callers queue at the configured spacing whatever N is.

The interval belongs to the call site, not to the queue: two places may space
themselves differently on one service (a cheap status ping and a content
download), and they still take their turns from the same queue. The sleep is
deliberately OUTSIDE the lock — it is the wait that must serialise, not the
arithmetic, and holding the lock through it would block callers of every other
service too.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
# service name → the monotonic instant at which the next call to it may be made.
_next_call_at: dict[str, float] = {}


def throttle(service: str, interval: float) -> None:
    """Wait until *service*'s next free slot, and reserve the one after it."""
    with _lock:
        slot = max(time.monotonic(), _next_call_at.get(service, 0.0))
        _next_call_at[service] = slot + max(interval, 0.0)
    remaining = slot - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
