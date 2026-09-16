"""In-memory sliding-window rate limiter, keyed by (bucket, ip)."""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque] = defaultdict(deque)

    def allow(self, bucket: str, key: str, limit: int, window_s: float) -> bool:
        """Record an attempt; return False when the limit is exceeded."""
        now = time.monotonic()
        dq = self._events[(bucket, key)]
        while dq and dq[0] <= now - window_s:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


limiter = RateLimiter()
