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
# "host#lane" entries are separate budgets so position monitoring (Risk) never waits in line
# behind discovery traffic. Lanes of one host add up to stay under that host's real limit.
HOST_LIMITS = {
    "api.dexscreener.com": 150,          # Scout / Filter / Analyst / Executor
    "api.dexscreener.com#risk": 60,      # open positions: priority lane
    "api.dexscreener.com#shadow": 60,    # 60-minute follow-up of evaluated tokens
    "api.dexscreener.com#revisit": 15,   # second look at archived (dropped) tokens
    "api.dexscreener.com#smart": 5,      # Whale checker: market-cap peaks of followed tokens (desk/smartwallets.py)
    "api.dexscreener.com#longwatch": 10, # rejected tokens followed for 24 h, one batch every 5 min (desk/longwatch.py)
    "api.dexscreener.com#entry": 10,     # approved tokens waiting for their entry moment (one batched call)
    "api.rugcheck.xyz": 12,
    "lite-api.jup.ag": 25,               # entry quotes
    "lite-api.jup.ag#risk": 30,          # exit-value quotes for open positions
    "lite-api.jup.ag#stop": 30,          # the one quote that confirms a stop before selling: never queued
    "lite-api.jup.ag#exec": 60,          # live orders/executes: rare, but never wait behind quotes
    "api.geckoterminal.com": 8,
    "api.mainnet-beta.solana.com": 90,
    "api.mainnet-beta.solana.com#smart": 90,  # smart-wallet scans (desk/smartwallets.py): own budget, never
                                              # in front of the Filter / DevWatch calls (public limit ~600/min)
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

    def limiter(self, host: str, lane: str = "") -> RateLimiter:
        key = f"{host}#{lane}" if lane else host
        if key not in self.limiters:
            self.limiters[key] = RateLimiter(HOST_LIMITS.get(key, HOST_LIMITS.get(host, 120)))
        return self.limiters[key]

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
        lane: str = "",
        timeout: Optional[float] = None,
    ) -> Any:
        host = urlparse(url).hostname or ""
        lim = self.limiter(host, lane)
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            await lim.wait()
            try:
                kw = {"timeout": timeout} if timeout else {}
                r = await self.client.request(method, url, json=json, params=params, **kw)
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
