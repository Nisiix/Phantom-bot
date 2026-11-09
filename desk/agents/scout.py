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

from ..clients.feeds import gecko_active_pools, gecko_new_pools, pumpportal_stream
from ..models import Candidate

SOL_MINT = "So11111111111111111111111111111111111111112"
STABLES = ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")
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
        if self.desk.gmgn is not None and self.cfg.gmgn.signal_feed:
            tasks.append(self.supervised("gmgn-signals", self.gmgn_loop))
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
            pools, failed = [], 0
            try:
                pools += await gecko_new_pools(self.desk.http, pages=1)
            except Exception:  # noqa: BLE001 - a flaky feed must not stop discovery
                failed += 1
            if n % 4 == 1:
                # Every ~80s: trending + top-volume pools. Tokens that are already moving are the ones that can
                # pass the $50k/h volume rule; new_pools alone mostly finds dust that never gets there.
                cutoff = time.time() - 48 * 3600
                try:
                    pools += [p for p in await gecko_active_pools(self.desk.http)
                              if p.get("created") and _iso(p["created"]) > cutoff
                              and p.get("volume_h1", 0) >= 0.5 * self.cfg.filter.min_volume_h1_usd]
                except Exception:  # noqa: BLE001
                    failed += 1
            for p in pools:
                if p["mint"] in (SOL_MINT, *STABLES):
                    continue
                self.discover(p["mint"], p["symbol"], "", f"{p['dex']}:pool", pair=p["pool"])
            if failed and not pools:
                backoff = min(backoff * 2, 300)
                self.state.bump("feed_errors")
                if backoff >= 160:
                    self.emit("warn", f"GeckoTerminal throttled; backing off {backoff:.0f}s")
            else:
                backoff = 20.0
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

    async def gmgn_loop(self) -> None:
        """Smart-money (type 12) and KOL (type 20) buy signals from GMGN."""
        from ..clients.gmgn import GmgnError
        names = {12: "smart", 20: "kol"}
        while not self.desk.stop.is_set():
            try:
                for sig in await self.desk.gmgn.signals([12, 20]):
                    kind = names.get(int(sig.get("signal_type") or 0), "signal")
                    self.discover(sig.get("token_address"), "?", "", f"gmgn:{kind}", urgent=True)
            except GmgnError as e:
                self.state.bump("feed_errors")
                self.emit("warn", f"GMGN signal feed: {e}")
            if not await self.sleep(45):
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

    async def _check_rug(self, mint: str, w: Watch) -> None:
        seen = self.desk.market_seen.get(mint)
        if not seen:
            return
        liq = seen[2]
        w.max_liq = max(w.max_liq, liq)
        if w.rug_flagged or w.max_liq < 10_000 or liq >= 0.2 * w.max_liq:
            return
        # A rug flag is permanent (blacklist + dev and funder in the rug ledger), so never act on one reading:
        # the batched DexScreener feed sometimes returns a wrong/thin pair. Confirm on the full pair list.
        try:
            full = await self.desk.dex.snapshot(mint)
        except Exception:  # noqa: BLE001 - can't confirm now, try again on the next pass
            return
        if full is not None:
            self.desk.market_seen[mint] = (time.time(), full.volume_h1, full.liquidity_usd)
            liq = full.liquidity_usd
            if liq >= 0.2 * w.max_liq:
                self.state.bump("rug_false_alarm")
                return
        w.rug_flagged = True
        creator = self.desk.creators.get(mint)
        reason = f"liquidity collapsed {w.max_liq:,.0f} -> {liq:,.0f} USD"
        if creator:
            self.desk.store.add_rugger(creator, mint, reason)
        funder = self.desk.funders.get(mint)
        if funder:
            self.desk.store.add_rugger(funder, mint, "funded a rugging dev: " + reason)
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
                await self._check_rug(mint, w)
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
