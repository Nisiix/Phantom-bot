"""MARKET REGIME — what BTC, SOL and the memecoin market were doing when the desk decided.

MEASUREMENT ONLY. Nothing here blocks or changes a trade. Every minute the desk reads, from Jupiter's
price API (the same free endpoint it already uses for SOL/USD):

  * BTC (Coinbase wrapped BTC on Solana, deep liquidity, tracks BTC) and SOL: price, 24h change,
    and the 1h change computed from our own one-minute price history;
  * a basket of large, established Solana memecoins: average 1h and 24h change ("meme index");

and, from the tokens the Filter is screening right now, the "launch breadth": the median 1h price
change and median 1h volume of the new tokens seen in the last 30 minutes.

That snapshot is stored with every shadow-tracked token and every real trade, so `run.py research`
can show whether the +12% / -6% hit rates really differ between calm markets and BTC/SOL crashes.
Only if the data shows a clear difference is it worth adding an entry block (starting with a wide
threshold that stops only crashes).
"""
from __future__ import annotations

import statistics
import time
from collections import deque

from .clients.jupiter import SOL_MINT

BTC_MINT = "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"   # cbBTC (Coinbase wrapped BTC)
MEME_BASKET = {
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": "WIF",
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr": "POPCAT",
    "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump": "FARTCOIN",
    "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN": "TRUMP",
    "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump": "PNUT",
}
TRACKED = [BTC_MINT, SOL_MINT, *MEME_BASKET]

HOUR = 3600.0
HOUR_TOLERANCE = 600.0       # a sample between 50 and 70 minutes old counts as "1h ago"
LAUNCH_WINDOW = 1800.0       # launch breadth: new tokens screened in the last 30 minutes
LAUNCH_MIN = 10              # ...and only when at least this many were seen


class MarketRegime:
    def __init__(self, jup=None, store=None) -> None:
        self.jup = jup
        self.store = store
        self.hist: dict[str, deque] = {m: deque(maxlen=240) for m in TRACKED}   # (ts, usd) once a minute
        self.chg24: dict[str, float] = {}
        self.launches: deque = deque(maxlen=5000)                               # (ts, chg_h1, vol_h1)
        self.updated = 0.0
        self.errors = 0
        if store is not None:     # keep the 1h window across restarts
            for m, pts in (store.kv_get("market_history", {}) or {}).items():
                if m in self.hist:
                    self.hist[m].extend(tuple(p) for p in pts[-240:])

    # ── inputs ──
    def record(self, prices: dict, t: float | None = None) -> None:
        """prices: Jupiter price/v3 response {mint: {"usdPrice": .., "priceChange24h": ..}}."""
        t = time.time() if t is None else t
        for mint in TRACKED:
            p = prices.get(mint) or {}
            usd = p.get("usdPrice")
            if usd:
                self.hist[mint].append((t, float(usd)))
            if p.get("priceChange24h") is not None:
                self.chg24[mint] = float(p["priceChange24h"])
        self.updated = t

    def note_launch(self, chg_h1, vol_h1, t: float | None = None) -> None:
        if isinstance(chg_h1, (int, float)) and isinstance(vol_h1, (int, float)):
            self.launches.append((time.time() if t is None else t, float(chg_h1), float(vol_h1)))

    async def refresh(self) -> float:
        """One Jupiter call for all tracked tokens. Returns the SOL/USD price (0 if unavailable)."""
        data = await self.jup.prices(TRACKED)
        self.record(data)
        if self.store is not None:
            self.store.kv_set("market_history", {m: list(h)[-90:] for m, h in self.hist.items()})
        return float((data.get(SOL_MINT) or {}).get("usdPrice") or 0.0)

    # ── outputs ──
    def change_1h(self, mint: str, t: float | None = None) -> float | None:
        h = self.hist.get(mint)
        if not h:
            return None
        t = time.time() if t is None else t
        now_ts, now_px = h[-1]
        if t - now_ts > 300:                     # stale feed: don't pretend we know
            return None
        target = now_ts - HOUR
        ts, px = min(h, key=lambda p: abs(p[0] - target))
        if abs(ts - target) > HOUR_TOLERANCE or px <= 0:
            return None
        return (now_px / px - 1) * 100

    def snapshot(self, t: float | None = None) -> dict:
        """Flat, JSON-safe regime features. Missing values are None (e.g. the first hour after start)."""
        t = time.time() if t is None else t
        meme_h1 = [c for c in (self.change_1h(m, t) for m in MEME_BASKET) if c is not None]
        meme_24 = [self.chg24[m] for m in MEME_BASKET if m in self.chg24]
        recent = [(c, v) for ts, c, v in self.launches if t - ts <= LAUNCH_WINDOW]
        fresh = t - self.updated <= 300
        out = {
            "mkt_btc_h1": self.change_1h(BTC_MINT, t),
            "mkt_sol_h1": self.change_1h(SOL_MINT, t),
            "mkt_meme_h1": statistics.fmean(meme_h1) if len(meme_h1) >= 3 else None,
            "mkt_btc_h24": self.chg24.get(BTC_MINT) if fresh else None,
            "mkt_sol_h24": self.chg24.get(SOL_MINT) if fresh else None,
            "mkt_meme_h24": statistics.fmean(meme_24) if fresh and len(meme_24) >= 3 else None,
            "mkt_launch_h1": statistics.median(c for c, _ in recent) if len(recent) >= LAUNCH_MIN else None,
            "mkt_launch_vol": statistics.median(v for _, v in recent) if len(recent) >= LAUNCH_MIN else None,
        }
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in out.items()}

    def short(self) -> str:
        s = self.snapshot()

        def f(v):
            return "n/a" if v is None else f"{v:+.1f}%"
        return f"BTC 1h {f(s['mkt_btc_h1'])}, SOL 1h {f(s['mkt_sol_h1'])}, memes 1h {f(s['mkt_meme_h1'])}"


# ── regime buckets used by the research report (fixed edges, so weeks stay comparable) ──
CRASH = {"mkt_btc_h1": -2.0, "mkt_sol_h1": -3.0, "mkt_meme_h1": -5.0}


def is_crash(f: dict) -> bool | None:
    """True if BTC, SOL or the meme index fell hard in the last hour. None if no regime data at all."""
    vals = {k: f.get(k) for k in CRASH}
    if all(v is None for v in vals.values()):
        return None
    return any(v is not None and v <= CRASH[k] for k, v in vals.items())
