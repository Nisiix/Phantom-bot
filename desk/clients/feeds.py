"""Discovery feeds: PumpPortal websocket (pump.fun) and GeckoTerminal new pools (Raydium & co)."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import websockets

from .http import Http

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana"
# GeckoTerminal dex ids we care about.
TRACKED_DEXES = ("raydium", "raydium-clmm", "raydium-launchlab", "pump-fun", "pumpswap", "letsbonk-fun")


async def pumpportal_stream(stop: asyncio.Event) -> AsyncIterator[dict]:
    """Yields create / migration events forever, reconnecting with backoff."""
    backoff = 2.0
    while not stop.is_set():
        try:
            async with websockets.connect(PUMPPORTAL_WS, ping_interval=20, ping_timeout=20,
                                          open_timeout=15, max_size=2**20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                backoff = 2.0
                async for raw in ws:
                    if stop.is_set():
                        return
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("mint"):
                        yield msg
        except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
            yield {"_error": f"pumpportal disconnected: {e.__class__.__name__}: {e}"}
        if stop.is_set():
            return
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


def _gecko_pool(p: dict) -> dict | None:
    a = p.get("attributes") or {}
    rel = p.get("relationships") or {}
    dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
    base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    if not base.startswith("solana_"):
        return None
    name = a.get("name") or ""
    return {
        "mint": base.split("_", 1)[1],
        "pool": a.get("address"),
        "dex": dex,
        "symbol": name.split(" / ")[0] if " / " in name else name,
        "created": a.get("pool_created_at"),
        "volume_h1": float((a.get("volume_usd") or {}).get("h1") or 0),
        "reserve_usd": float(a.get("reserve_in_usd") or 0),
    }


async def gecko_new_pools(http: Http, pages: int = 2) -> list[dict]:
    out = []
    for page in range(1, pages + 1):
        data = await http.get(f"{GECKO}/new_pools", params={"page": page}, retries=1) or {}
        for p in data.get("data") or []:
            pool = _gecko_pool(p)
            if pool and pool["dex"] in TRACKED_DEXES:
                out.append(pool)
    return out


async def gecko_active_raydium(http: Http) -> list[dict]:
    """Raydium pools by 24h volume — catches young pools that slipped past new_pools."""
    out = []
    for dex in ("raydium", "raydium-launchlab"):
        data = await http.get(f"{GECKO}/dexes/{dex}/pools", params={"page": 1, "sort": "h24_volume_usd_desc"},
                              retries=1) or {}
        for p in data.get("data") or []:
            pool = _gecko_pool(p)
            if pool:
                out.append(pool)
    return out
