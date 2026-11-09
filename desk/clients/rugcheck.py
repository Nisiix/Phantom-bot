"""RugCheck: authorities, LP lock, holders, creator history, insider networks."""
from __future__ import annotations

from .http import Http, TTLCache

BASE = "https://api.rugcheck.xyz/v1"


class RugCheck:
    def __init__(self, http: Http) -> None:
        self.http = http
        self.cache = TTLCache(ttl=240.0, max_items=3000)

    async def report(self, mint: str, fresh: bool = False) -> dict | None:
        if not fresh:
            hit = self.cache.get(mint)
            if hit is not None:
                return hit or None
        data = await self.http.get(f"{BASE}/tokens/{mint}/report", retries=2)
        self.cache.set(mint, data or {})
        return data or None

    async def new_tokens(self) -> list[dict]:
        data = await self.http.get(f"{BASE}/stats/new_tokens", retries=1)
        return data if isinstance(data, list) else []
