"""Shared HTTP plumbing: one async client, per-host rate limiting, retries, TTL cache."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

UA = "PhantomDesk/1.0 (+https://github.com/nisiix/phantom-bot)"


class RateLimiter:
    """Simple spacing limiter: at most one request every `interval` seconds."""

    def __init__(self, per_minute: float) -> None:
        self.interval = 60.0 / per_minute
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            t = time.monotonic()
            if t < self._next:
                await asyncio.sleep(self._next - t)
                t = time.monotonic()
            self._next = t + self.interval

    def penalize(self, seconds: float) -> None:
        self._next = max(self._next, time.monotonic() + seconds)


class TTLCache:
    def __init__(self, ttl: float, max_items: int = 5000) -> None:
        self.ttl = ttl
        self.max = max_items
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        hit = self._d.get(key)
        if not hit:
            return None
        ts, val = hit
        if time.monotonic() - ts > self.ttl:
            self._d.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any) -> None:
        if len(self._d) >= self.max:
            for k in list(self._d)[: self.max // 5]:
                self._d.pop(k, None)
        self._d[key] = (time.monotonic(), val)


# Requests per minute per host (conservative vs. published free-tier limits).
HOST_LIMITS = {
    "api.dexscreener.com": 240,
    "api.rugcheck.xyz": 12,
    "lite-api.jup.ag": 50,
    "api.geckoterminal.com": 8,
    "api.mainnet-beta.solana.com": 90,
}


class Http:
    def __init__(self, timeout: float = 15.0) -> None:
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": UA, "Accept": "application/json"},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        )
        self.limiters: dict[str, RateLimiter] = {}
        self.stats: dict[str, dict[str, int]] = {}

    def limiter(self, host: str) -> RateLimiter:
        if host not in self.limiters:
            self.limiters[host] = RateLimiter(HOST_LIMITS.get(host, 120))
        return self.limiters[host]

    def _stat(self, host: str, key: str) -> None:
        self.stats.setdefault(host, {"ok": 0, "err": 0, "429": 0})[key] += 1

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: Optional[dict] = None,
        retries: int = 3,
        expect_json: bool = True,
    ) -> Any:
        host = urlparse(url).hostname or ""
        lim = self.limiter(host)
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            await lim.wait()
            try:
                r = await self.client.request(method, url, json=json, params=params)
                if r.status_code == 429:
                    self._stat(host, "429")
                    wait = float(r.headers.get("retry-after", 0) or 0) or (2 ** attempt) * 2
                    lim.penalize(min(wait, 60))
                    last_exc = httpx.HTTPStatusError("429", request=r.request, response=r)
                    continue
                if r.status_code >= 500:
                    self._stat(host, "err")
                    last_exc = httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
                    await asyncio.sleep((2 ** attempt) * 0.5 + random.random() * 0.3)
                    continue
                if r.status_code == 404:
                    self._stat(host, "ok")
                    return None
                r.raise_for_status()
                self._stat(host, "ok")
                return r.json() if expect_json else r
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as e:
                self._stat(host, "err")
                last_exc = e
                await asyncio.sleep((2 ** attempt) * 0.5 + random.random() * 0.3)
        raise RuntimeError(f"{method} {host} failed after {retries + 1} tries: {last_exc}")

    async def get(self, url: str, **kw) -> Any:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> Any:
        return await self.request("POST", url, **kw)

    async def close(self) -> None:
        await self.client.aclose()
