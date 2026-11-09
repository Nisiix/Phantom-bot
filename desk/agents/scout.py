"""SCOUT — discovers new pairs on pump.fun and Raydium in real time.

Sources
  * PumpPortal websocket: every pump.fun launch + every graduation/migration (push, real time)
  * GeckoTerminal new_pools: newest pools on Raydium / Raydium LaunchLab / PumpSwap / pump.fun
  * GeckoTerminal Raydium pools by volume: young Raydium pools that are already trading
  * DexScreener latest token profiles & boosts (Solana)

The Scout never judges quality. It keeps a watchlist and keeps re-submitting
tokens to the Filter (young tokens rarely have $50k/h volume on minute one),
and it drops tokens that are dead, blacklisted, too old or already traded.
It also feeds the rug ledger: if a watched token's liquidity collapses, its
creator is recorded as a rugger so the Filter can kill their future launches.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ..clients.feeds import gecko_active_raydium, gecko_new_pools, pumpportal_stream
from ..models import Candidate
from .base import Agent


@dataclass
class Watch:
    cand: Candidate
    added: float
    last_sent: float = 0.0
    max_liq: float = 0.0
    rug_flagged: bool = False


class Scout(Agent):
    name = "Scout"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.watch: dict[str, Watch] = {}
        self.c = self.cfg.scout

    # ── intake ──
    def discover(self, mint: str, symbol: str = "?", name: str = "", source: str = "",
                 creator: str | None = None, pair: str | None = None, urgent: bool = False) -> None:
        if not mint or mint in self.desk.blacklist:
            return
        if creator:
            self.desk.creators.setdefault(mint, creator)
        w = self.watch.get(mint)
        if w:
            if urgent:  # e.g. migration to an AMM -> re-check immediately
                w.last_sent = 0.0
                w.cand.source = source
            return
        if len(self.watch) >= self.c.max_watchlist:
            self._prune(force=True)
            if len(self.watch) >= self.c.max_watchlist:
                return
        self.watch[mint] = Watch(Candidate(mint, symbol or "?", name, source, creator=creator, pair_address=pair),
                                 added=time.time())
        self.state.bump("discovered")
        self.state.bump(f"src:{source.split(':')[0]}")

    # ── loops ──
    async def run(self) -> None:
        self.status("working")
        tasks = [self.supervised("recheck", self.recheck_loop)]
        if self.c.pumpportal:
            tasks.append(self.supervised("pumpportal", self.pumpportal_loop))
        if self.c.geckoterminal:
            tasks.append(self.supervised("geckoterminal", self.gecko_loop))
        if self.c.dexscreener:
            tasks.append(self.supervised("dexscreener", self.dexscreener_loop))
        self.emit("info", f"Scout online: sources pumpportal={self.c.pumpportal} "
                          f"gecko={self.c.geckoterminal} dexscreener={self.c.dexscreener}")
        await asyncio.gather(*tasks)

    async def pumpportal_loop(self) -> None:
        async for msg in pumpportal_stream(self.desk.stop):
            if "_error" in msg:
                self.emit("warn", msg["_error"])
                continue
            tx = msg.get("txType")
            if tx == "create":
                self.discover(msg["mint"], msg.get("symbol", "?"), msg.get("name", ""), "pumpfun:new",
                              creator=msg.get("traderPublicKey"))
            elif tx == "migrate" or "pool" in msg and tx != "create":
                self.discover(msg["mint"], msg.get("symbol", "?"), msg.get("name", ""), "pumpfun:migration",
                              urgent=True)
                self.emit("info", "pump.fun graduation detected", msg["mint"], msg.get("symbol", ""))

    async def gecko_loop(self) -> None:
        """GeckoTerminal's free API is ~30 req/min shared per IP: poll gently, back off on 429."""
        n, backoff = 0, 20.0
        while not self.desk.stop.is_set():
            try:
                pools = await gecko_new_pools(self.desk.http, pages=1)
                if n % 10 == 5:
                    cutoff = time.time() - 48 * 3600
                    pools += [p for p in await gecko_active_raydium(self.desk.http)
                              if p.get("created") and _iso(p["created"]) > cutoff]
                for p in pools:
                    self.discover(p["mint"], p["symbol"], "", f"{p['dex']}:pool", pair=p["pool"])
                backoff = 20.0
            except Exception as e:  # noqa: BLE001 - a flaky feed must not stop discovery
                backoff = min(backoff * 2, 300)
                self.state.bump("feed_errors")
                if backoff >= 160:
                    self.emit("warn", f"GeckoTerminal throttled ({e.__class__.__name__}); backing off {backoff:.0f}s")
            n += 1
            if not await self.sleep(backoff):
                return

    async def dexscreener_loop(self) -> None:
        while not self.desk.stop.is_set():
            try:
                for d in await self.desk.dex.latest_profiles():
                    self.discover(d.get("tokenAddress"), "?", "", "dexscreener:profile")
            except Exception:  # noqa: BLE001
                self.state.bump("feed_errors")
            if not await self.sleep(30):
                return

    def _prune(self, force: bool = False) -> None:
        t = time.time()
        held = self.desk.portfolio.positions
        for mint, w in list(self.watch.items()):
            seen = self.desk.market_seen.get(mint)
            age_min = (t - w.added) / 60
            dead = False
            if mint in self.desk.blacklist or mint in held:
                dead = True
            elif age_min > self.c.watch_minutes:
                dead = True
            elif seen is None and age_min > 15:
                dead = True                     # never got a market
            elif seen and seen[1] < 1_000 and age_min > 20:
                dead = True                     # dormant, <$1k/h after 20 min
            elif force and seen and seen[1] < 10_000:
                dead = True
            if dead:
                self.watch.pop(mint, None)
                self.state.bump("dropped")

    def _check_rug(self, mint: str, w: Watch) -> None:
        seen = self.desk.market_seen.get(mint)
        if not seen:
            return
        liq = seen[2]
        w.max_liq = max(w.max_liq, liq)
        if not w.rug_flagged and w.max_liq >= 10_000 and liq < 0.2 * w.max_liq:
            w.rug_flagged = True
            creator = self.desk.creators.get(mint)
            reason = f"liquidity collapsed {w.max_liq:,.0f} -> {liq:,.0f} USD"
            if creator:
                self.desk.store.add_rugger(creator, mint, reason)
            self.desk.blacklist[mint] = "rugged: " + reason
            self.emit("warn", f"RUG observed ({reason}); dev {('added to rug ledger: ' + creator[:6]) if creator else 'unknown'}",
                      mint, w.cand.symbol)

    async def recheck_loop(self) -> None:
        last_prune = 0.0
        while not self.desk.stop.is_set():
            t = time.time()
            if t - last_prune > 20:
                self._prune()
                last_prune = t
            sent = 0
            for mint, w in list(self.watch.items()):
                if mint in self.desk.in_flight:
                    continue
                self._check_rug(mint, w)
                seen = self.desk.market_seen.get(mint)
                vol = seen[1] if seen else 0
                interval = self.c.recheck_seconds
                if vol >= 25_000:
                    interval = min(interval, 20)
                elif seen and vol < 5_000:
                    interval = max(interval, 120)
                if t - w.last_sent < interval:
                    continue
                if self.desk.candidates.full():
                    break
                w.last_sent = t
                w.cand.attempts += 1
                self.desk.candidates.put_nowait(w.cand)
                sent += 1
            if sent:
                self.state.bump("sent", sent)
            self.state.counters["watching"] = len(self.watch)
            self.state.last = f"watching {len(self.watch)} tokens, queued {sent}"
            if not await self.sleep(3):
                return


def _iso(s: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
