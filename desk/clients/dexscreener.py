"""DexScreener: market data (price, liquidity, volume, txns, socials)."""
from __future__ import annotations

from typing import Iterable

from ..models import MarketSnapshot
from .http import Http, TTLCache

BASE = "https://api.dexscreener.com"
SOL = "So11111111111111111111111111111111111111112"
USD_QUOTES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _i(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def snapshot_from_pairs(mint: str, pairs: list[dict]) -> MarketSnapshot | None:
    """Aggregate all Solana pairs where `mint` is the base token."""
    pairs = [
        p for p in pairs
        if p.get("chainId") == "solana" and (p.get("baseToken") or {}).get("address") == mint
    ]
    if not pairs:
        return None
    pairs.sort(key=lambda p: _f((p.get("liquidity") or {}).get("usd")), reverse=True)
    main = pairs[0]
    # Prefer the deepest SOL-quoted pair as the "main" pair for pricing.
    for p in pairs:
        if (p.get("quoteToken") or {}).get("address") == SOL:
            main = p
            break

    def tx(p, w, side):
        return _i(((p.get("txns") or {}).get(w) or {}).get(side))

    vol_h1 = sum(_f((p.get("volume") or {}).get("h1")) for p in pairs)
    vol_m5 = sum(_f((p.get("volume") or {}).get("m5")) for p in pairs)
    liq = sum(_f((p.get("liquidity") or {}).get("usd")) for p in pairs)
    info = main.get("info") or {}
    websites = [w.get("url") for w in (info.get("websites") or []) if w.get("url")]
    socials = {}
    for s in info.get("socials") or []:
        t, u = s.get("type"), s.get("url")
        if t and u and t not in socials:
            socials[t] = u
    pc = main.get("priceChange") or {}
    quote = (main.get("quoteToken") or {}).get("address")
    created = _f(main.get("pairCreatedAt")) / 1000.0
    # The earliest pair is the token's real "birth" on a DEX.
    births = [_f(p.get("pairCreatedAt")) / 1000.0 for p in pairs if p.get("pairCreatedAt")]
    if births:
        created = min(births)
    base = main.get("baseToken") or {}
    return MarketSnapshot(
        mint=mint,
        symbol=base.get("symbol") or "?",
        name=base.get("name") or "",
        pair_address=main.get("pairAddress") or "",
        dex_id=main.get("dexId") or "",
        price_usd=_f(main.get("priceUsd")),
        price_native=_f(main.get("priceNative")) if quote == SOL else 0.0,
        liquidity_usd=liq,
        volume_h1=vol_h1,
        volume_m5=vol_m5,
        buys_h1=sum(tx(p, "h1", "buys") for p in pairs),
        sells_h1=sum(tx(p, "h1", "sells") for p in pairs),
        buys_m5=sum(tx(p, "m5", "buys") for p in pairs),
        sells_m5=sum(tx(p, "m5", "sells") for p in pairs),
        change_m5=_f(pc.get("m5")),
        change_h1=_f(pc.get("h1")),
        change_h6=_f(pc.get("h6")),
        fdv=_f(main.get("fdv")),
        market_cap=_f(main.get("marketCap")),
        pair_created_at=created,
        websites=websites,
        socials=socials,
        pair_count=len(pairs),
    )


class DexScreener:
    def __init__(self, http: Http) -> None:
        self.http = http
        self.cache = TTLCache(ttl=8.0)

    async def snapshots(self, mints: Iterable[str], fresh: bool = False, lane: str = "") -> dict[str, MarketSnapshot]:
        """Batch fetch (30 mints per request). Returns only mints with market data."""
        mints = list(dict.fromkeys(m for m in mints if m))
        out: dict[str, MarketSnapshot] = {}
        todo = []
        for m in mints:
            hit = None if fresh else self.cache.get(m)
            if hit is not None:
                if hit != "none":
                    out[m] = hit
            else:
                todo.append(m)
        for i in range(0, len(todo), 30):
            chunk = todo[i: i + 30]
            data = await self.http.get(f"{BASE}/tokens/v1/solana/{','.join(chunk)}", lane=lane) or []
            by_mint: dict[str, list[dict]] = {}
            for p in data if isinstance(data, list) else []:
                by_mint.setdefault((p.get("baseToken") or {}).get("address"), []).append(p)
            for m in chunk:
                snap = snapshot_from_pairs(m, by_mint.get(m, []))
                self.cache.set(m, snap if snap else "none")
                if snap:
                    out[m] = snap
        return out

    async def snapshot(self, mint: str, fresh: bool = True, lane: str = "") -> MarketSnapshot | None:
        """One token, ALL its pairs. The batched /tokens/v1 endpoint returns a single pair per token, and now
        and then a wrong/thin one (seen live: a $1.6M pool read as $1,936). Use this for anything that acts on
        the numbers (Analyst score, Executor checks, rug / liquidity-pull confirmation)."""
        if not fresh:
            hit = self.cache.get(mint)
            if hit is not None:
                return hit if hit != "none" else None
        data = await self.http.get(f"{BASE}/token-pairs/v1/solana/{mint}", lane=lane)
        if not isinstance(data, list):
            return (await self.snapshots([mint], fresh=fresh, lane=lane)).get(mint)
        snap = snapshot_from_pairs(mint, data)
        self.cache.set(mint, snap if snap else "none")
        return snap

    async def latest_profiles(self) -> list[dict]:
        out = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
            data = await self.http.get(BASE + path) or []
            out.extend(d for d in data if isinstance(d, dict) and d.get("chainId") == "solana")
        return out
