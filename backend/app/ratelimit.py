"""Per-client token bucket for the endpoints that cost money (model calls, web searches, storage).

Sessions are free and anonymous, so per-session limits would do nothing against a script that
simply makes new sessions; the bucket is keyed by client IP instead. Routes opt in with a cost::

    @router.post("/design/{session_id}/generate", dependencies=[limit(3)])

Defaults are generous enough that a real player never notices (a full journey costs ~30 tokens
against a burst of 60, refilled at 20 per minute). Tune with TEE_RATE_LIMIT_BURST and
TEE_RATE_LIMIT_PER_MIN; TEE_RATE_LIMIT_PER_MIN=0 disables limiting. Behind a reverse proxy, run
uvicorn with ``--proxy-headers --forwarded-allow-ips=<proxy>`` so the real client IP is used;
otherwise every visitor shares the proxy's bucket. This is in-memory and per process: it slows
abuse down, it doesn't replace a spend limit on the Portkey key.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import OrderedDict

from fastapi import Depends, Request

from .errors import RateLimited

__all__ = ["TokenBucket", "bucket", "charge", "client_key", "limit"]

MAX_CLIENTS = 10_000  # remembered buckets (least recently used are forgotten)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


class TokenBucket:
    """``burst`` tokens per key, refilled continuously at ``per_minute``. Thread-safe."""

    def __init__(self, burst: float, per_minute: float) -> None:
        self.burst = burst
        self.per_minute = per_minute
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()  # key -> (tokens, at)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0 and self.burst > 0

    def take(self, key: str, cost: float = 1.0) -> float:
        """Spend ``cost`` tokens. Returns 0 when allowed, else the seconds until it would be."""
        if not self.enabled:
            return 0.0
        now = time.monotonic()
        rate = self.per_minute / 60.0
        with self._lock:
            tokens, at = self._buckets.pop(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - at) * rate)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                wait = 0.0
            else:
                self._buckets[key] = (tokens, now)
                wait = (cost - tokens) / rate
            while len(self._buckets) > MAX_CLIENTS:
                self._buckets.popitem(last=False)
        return wait

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


bucket = TokenBucket(
    burst=_env_float("TEE_RATE_LIMIT_BURST", 60.0),
    per_minute=_env_float("TEE_RATE_LIMIT_PER_MIN", 20.0),
)


def client_key(request: Request) -> str:
    """The caller's identity for limits: its IP (see the module note about reverse proxies)."""
    return request.client.host if request.client else "unknown"


def charge(request: Request, cost: float) -> None:
    """Charge ``cost`` tokens to the caller's IP, or raise RateLimited (429). For costs that are
    only known once the request is parsed (e.g. how many documents an upload holds)."""
    wait = bucket.take(client_key(request), cost)
    if wait:
        raise RateLimited(retry_after=math.ceil(wait))


def limit(cost: float):
    """FastAPI dependency: charge ``cost`` tokens to the caller's IP, or answer 429."""

    async def _charge(request: Request) -> None:
        charge(request, cost)

    return Depends(_charge)
