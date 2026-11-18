"""Best-effort per-IP sliding window. Each serverless instance keeps its own memory."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    """Allow `limit` hits per IP inside a rolling window of `window_s` seconds."""

    def __init__(self, limit: int, window_s: int = 60) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> tuple[bool, int]:
        """Return whether the call may proceed and a Retry-After hint in seconds."""
        now = time.monotonic()
        bucket = self._hits[key]
        cutoff = now - self.window_s
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            retry = max(1, int(self.window_s - (now - bucket[0])) + 1)
            return False, retry
        bucket.append(now)
        return True, 0

    def reset(self) -> None:
        """Drop every bucket. Tests use this between cases."""
        self._hits.clear()
