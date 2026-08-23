"""Dependency-free token-bucket rate limiting shared by Cascade services.

The limiter is per-client-key (client IP by default), refills continuously,
and takes an injectable clock so tests can advance time deterministically.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any, Callable, Iterable


class TokenBucketRateLimiter:
    """Per-key token bucket. Capacity tokens, refilled continuously over window_seconds."""

    def __init__(
        self,
        *,
        capacity: int = 30,
        window_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
        max_tracked_keys: int = 10_000,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_tracked_keys <= 0:
            raise ValueError("max_tracked_keys must be positive")
        self.capacity = float(capacity)
        self.window_seconds = float(window_seconds)
        self.max_tracked_keys = max_tracked_keys
        self.clock = clock or time.monotonic
        self._state: OrderedDict[str, tuple[float, float]] = {}

    @property
    def refill_per_second(self) -> float:
        return self.capacity / self.window_seconds

    def try_acquire(self, key: str) -> bool:
        """Consume one token for key; return False when the bucket is empty."""
        now = self.clock()
        tokens, last = self._state.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + max(0.0, now - last) * self.refill_per_second)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._state[key] = (tokens, now)
        self._evict_stale(now)
        return allowed

    def retry_after_seconds(self) -> float:
        """Seconds until a full token is available for an empty bucket."""
        return 1.0 / self.refill_per_second

    def reset(self) -> None:
        self._state.clear()

    def _evict_stale(self, now: float) -> None:
        while len(self._state) > self.max_tracked_keys:
            self._state.popitem(last=False)


def client_key(scope: dict[str, Any], forwarded_header: str = "X-Forwarded-For") -> str:
    """Best-effort client identity: first forwarded hop, else the ASGI client."""
    headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in scope.get("headers", [])
    }
    forwarded = headers.get(forwarded_header.lower(), "").split(",")[0].strip()
    if forwarded:
        return forwarded[:128]
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])[:128]
    return "unknown"


class RateLimitMiddleware:
    """Pure ASGI middleware enforcing a token bucket on selected method/path pairs.

    Responses are FastAPI-style ``{"detail": ...}`` bodies with status 429 and a
    Retry-After header, matching the services' HTTPException error style.
    """

    def __init__(
        self,
        app: Any,
        *,
        limiter: TokenBucketRateLimiter,
        routes: Iterable[tuple[str, str]],
        message: str = "Request rate limit exceeded",
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.routes = {(method.upper(), path) for method, path in routes}
        self.message = message

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("method"):
            await self.app(scope, receive, send)
            return
        route = (scope["method"].upper(), scope.get("path", ""))
        if route not in self.routes or self.limiter.try_acquire(client_key(scope)):
            await self.app(scope, receive, send)
            return
        retry_after = max(1, round(self.limiter.retry_after_seconds()))
        body = json.dumps({"detail": self.message}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(retry_after).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
