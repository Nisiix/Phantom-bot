"""GMGN OpenAPI (read-only) — wallet intelligence the free APIs don't have.

Used for: bundler / sniper / insider / rat-trader shares, GMGN's rug label, honeypot flag,
dev funding source (1 hop), dev launch history, smart-money & KOL wallet counts, holder
funding clusters, and a smart-money / KOL buy-signal feed for the Scout.

Deliberately NOT used: GMGN swap / order / strategy routes. They execute from a wallet that
GMGN holds server-side. This desk signs every transaction locally with your own key.

Auth: header X-APIKEY plus `timestamp` (unix seconds, ±5 s) and `client_id` (uuid4, unique per
7 s) query params. Responses are {code, data, message|error}; code != 0 is an error.
Rate limit: weighted leaky bucket per plan. On 429 we stop ALL calls until reset_at
(early retries extend GMGN's ban). Needs a paid plan (Plus or better) for full checks.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx

BASE = "https://openapi.gmgn.ai"
PLANS = {"free": (5, 5), "plus": (20, 20), "pro": (50, 50)}   # (rate per second, capacity)
WEIGHTS = {"/v1/token/info": 1, "/v1/token/security": 1, "/v1/market/token_top_holders": 5,
           "/v1/user/created_tokens": 2, "/v1/market/token_signal": 1}


class GmgnError(RuntimeError):
    pass


class WeightedBucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self.rate, self.capacity = rate, capacity
        self.level = 0.0
        self.t = time.monotonic()
        self.blocked_until = 0.0     # wall clock
        self.lock = asyncio.Lock()

    async def take(self, weight: float) -> None:
        async with self.lock:
            while True:
                wait = self.blocked_until - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                now = time.monotonic()
                self.level = max(0.0, self.level - (now - self.t) * self.rate)
                self.t = now
                if self.level + weight <= self.capacity:
                    self.level += weight
                    return
                await asyncio.sleep((self.level + weight - self.capacity) / self.rate)

    def block(self, until_ts: float) -> None:
        self.blocked_until = max(self.blocked_until, until_ts)


class Gmgn:
    def __init__(self, api_key: str, plan: str = "plus") -> None:
        self.key = api_key
        rate, cap = PLANS.get(plan, PLANS["plus"])
        self.bucket = WeightedBucket(rate, cap)
        self.unavailable: set[str] = set()     # routes our plan can't call (403 PLAN_UPGRADE_REQUIRED)
        kw = {}
        if not (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")):
            # GMGN rejects IPv6 clients: bind to IPv4.
            kw["transport"] = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1)
        self.client = httpx.AsyncClient(timeout=15, headers={"X-APIKEY": api_key,
                                                             "User-Agent": "PhantomDesk/1.0"}, **kw)
        self.calls = 0
        self.errors = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        if path in self.unavailable:
            return None
        await self.bucket.take(WEIGHTS.get(path, 1))
        q = dict(params or {})
        q["timestamp"] = str(int(time.time()))
        q["client_id"] = str(uuid.uuid4())
        self.calls += 1
        try:
            r = await self.client.request(method, BASE + path, params=q, json=body)
        except httpx.HTTPError as e:
            self.errors += 1
            raise GmgnError(f"{path}: {e.__class__.__name__}") from e
        if r.status_code == 429:
            self.errors += 1
            reset = r.headers.get("X-RateLimit-Reset")
            try:
                reset_at = float(reset) if reset else float((r.json() or {}).get("reset_at") or 0)
            except ValueError:
                reset_at = 0.0
            self.bucket.block(reset_at if reset_at > time.time() else time.time() + 30)
            raise GmgnError(f"{path}: rate limited until {reset_at:.0f}")
        if r.status_code == 403 and "PLAN_UPGRADE_REQUIRED" in r.text:
            self.unavailable.add(path)
            raise GmgnError(f"{path}: not included in this GMGN plan")
        if r.status_code >= 400:
            self.errors += 1
            raise GmgnError(f"{path}: HTTP {r.status_code}")
        data = r.json()
        if not isinstance(data, dict) or data.get("code", 0) != 0:
            self.errors += 1
            raise GmgnError(f"{path}: {data.get('message') or data.get('error') if isinstance(data, dict) else data}")
        return data.get("data")

    # ── routes ──
    async def token_info(self, mint: str) -> dict | None:
        return await self._call("GET", "/v1/token/info", {"chain": "sol", "address": mint})

    async def token_security(self, mint: str) -> dict | None:
        return await self._call("GET", "/v1/token/security", {"chain": "sol", "address": mint})

    async def top_holders(self, mint: str, limit: int = 100) -> list[dict]:
        d = await self._call("GET", "/v1/market/token_top_holders",
                             {"chain": "sol", "address": mint, "limit": str(limit),
                              "order_by": "amount_percentage", "direction": "desc"})
        return (d or {}).get("list") or [] if isinstance(d, dict) else (d or [])

    async def created_tokens(self, wallet: str) -> dict | None:
        return await self._call("GET", "/v1/user/created_tokens",
                                {"chain": "sol", "wallet_address": wallet, "order_by": "token_ath_mc",
                                 "direction": "desc"})

    async def signals(self, types: list[int], mc_min: float = 0) -> list[dict]:
        group: dict = {"signal_type": types}
        if mc_min:
            group["mc_min"] = mc_min
        d = await self._call("POST", "/v1/market/token_signal", body={"chain": "sol", "groups": [group]})
        if isinstance(d, dict):
            return d.get("list") or []
        return d or []


# ── analysis (pure functions, unit-tested) ───────────────────────────────────
def _f(x) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def funding_clusters(holders: list[dict], mint: str) -> dict:
    """Holders (wallets only) grouped by the wallet that first funded them with SOL.

    A group of >= 3 is "coherent" when its funding times fall within 6 h or every funded
    amount is within 15% of the group mean. Also flags scripted funding: >= 3 fundings
    inside a 60 s span (sliding 300 s window). Returns % of total supply held by clusters.
    """
    wallets = [h for h in holders if int(h.get("addr_type") or 0) == 0
               and (h.get("address") or "").lower() != mint.lower()]
    groups: dict[str, list[dict]] = {}
    for h in wallets:
        nt = h.get("native_transfer") or {}
        src = nt.get("from_address")
        if src:
            groups.setdefault(src, []).append(h)
    clusters, cluster_pct = [], 0.0
    for src, hs in groups.items():
        if len(hs) < 3:
            continue
        ts = [float((h.get("native_transfer") or {}).get("timestamp") or 0) for h in hs]
        amts = [float((h.get("native_transfer") or {}).get("amount") or 0) for h in hs]
        mean = sum(amts) / len(amts) if amts else 0
        coherent = (max(ts) - min(ts) <= 6 * 3600 and min(ts) > 0) or \
                   (mean > 0 and all(abs(a - mean) <= 0.15 * mean for a in amts))
        if coherent:
            pct = sum(float(h.get("amount_percentage") or 0) for h in hs) * 100
            cluster_pct += pct
            clusters.append({"funder": src, "wallets": len(hs), "pct": round(pct, 2)})
    all_ts = sorted(float((h.get("native_transfer") or {}).get("timestamp") or 0) for h in wallets
                    if (h.get("native_transfer") or {}).get("timestamp"))
    synced = False
    j = 0
    for i in range(len(all_ts)):
        while all_ts[i] - all_ts[j] > 300:
            j += 1
        window = all_ts[j: i + 1]
        if len(window) >= 3 and window[-1] - window[0] <= 60:
            synced = True
            break
    return {"cluster_pct": round(cluster_pct, 2), "clusters": sorted(clusters, key=lambda c: -c["pct"])[:5],
            "synced_funding": synced, "wallets_seen": len(wallets)}


def summarize(info: dict | None, sec: dict | None) -> dict:
    """Flatten the GMGN fields we use. Missing values stay None (= unknown, never 'safe')."""
    info, sec = info or {}, sec or {}
    dev, stat, tags = info.get("dev") or {}, info.get("stat") or {}, info.get("wallet_tags_stat") or {}
    return {
        "rug_ratio": _f(sec.get("rug_ratio")),
        "honeypot": sec.get("honeypot"),
        "can_not_sell": sec.get("can_not_sell"),
        "is_wash_trading": sec.get("is_wash_trading"),
        "insider_rate": _f(sec.get("suspected_insider_hold_rate")),
        "bundler_rate": _f(sec.get("bundler_trader_amount_rate")) or _f(stat.get("top_bundler_trader_percentage")),
        "rat_rate": _f(sec.get("rat_trader_amount_rate")) or _f(stat.get("top_rat_trader_percentage")),
        "sniper_rate": _f(stat.get("top70_sniper_hold_rate")),
        "fresh_wallet_rate": _f(stat.get("fresh_wallet_rate")),
        "bot_degen_rate": _f(stat.get("bot_degen_rate")),
        "dev_team_hold_rate": _f(stat.get("dev_team_hold_rate")),
        "creator": dev.get("creator_address"),
        "dev_funder": dev.get("fund_from"),
        "dev_status": dev.get("creator_token_status"),
        "dev_open_count": dev.get("creator_open_count"),
        "smart_wallets": _f(tags.get("smart_wallets")),
        "renowned_wallets": _f(tags.get("renowned_wallets")),
        "sniper_wallets": _f(tags.get("sniper_wallets")),
        "holder_count": _f(info.get("holder_count")),
    }


def dev_history(created: dict | None) -> dict:
    created = created or {}
    inner = int(created.get("inner_count") or 0)
    opened = int(created.get("open_count") or 0)
    launches = inner + opened
    ratio = _f(created.get("open_ratio"))
    if ratio is None and launches:
        ratio = opened / launches
    return {"launches": launches, "graduated": opened, "open_ratio": ratio}
