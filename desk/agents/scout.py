"""SCOUT — discovers new pairs on pump.fun (and, with [scout] venues = "all", Raydium & co) in real time.

Sources
  * PumpPortal websocket: every pump.fun launch + every graduation/migration (push, real time)
  * GeckoTerminal new_pools: newest pools on pump.fun / PumpSwap (venues = "all": also Raydium / LaunchLab)
  * GeckoTerminal trending + top-volume pools: young pools that are already trading
  * DexScreener latest token profiles & boosts (Solana)

The Scout never judges quality. It keeps a watchlist and keeps re-submitting
tokens to the Filter (young tokens rarely have $50k/h volume on minute one),
and it drops tokens that are dead, blacklisted, too old or already traded.

Second look: a dropped token is not forgotten. Unless it was killed for good (blacklist), it goes to an
archive that is re-read every [scout] revisit_seconds for revisit_hours after discovery. When it trades again
(last-hour volume and liquidity above the revisit bars) it goes back on the watchlist and through the whole
Filter -> Analyst -> Risk chain, so a token excluded at launch can still be bought later in its life.
A graduation also restarts the watch window: the 45-minute post-graduation wait must not eat it.
It also feeds the rug ledger: if a watched token's liquidity collapses, its
creator is recorded as a rugger so the Filter can kill their future launches.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ..clients.feeds import gecko_active_pools, gecko_new_pools, pumpportal_stream, venue_dexes
from .. import rugnames
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
    first_seen: float = 0.0       # first discovery (the archive window counts from here)
    revived: int = 0              # times brought back by the second look


@dataclass
class Archived:
    cand: Candidate
    first_seen: float
    dropped: float
    why: str
    max_liq: float = 0.0
    revived: int = 0


class Scout(Agent):
    name = "Scout"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.watch: dict[str, Watch] = {}
        self.archive: dict[str, Archived] = {}     # second look: dropped tokens, re-read every revisit_seconds
        self.c = self.cfg.scout

    # ── intake ──
    def discover(self, mint: str, symbol: str = "?", name: str = "", source: str = "",
                 creator: str | None = None, pair: str | None = None, urgent: bool = False) -> None:
        if not mint or mint in self.desk.blacklist:
            return
        from ..controller import source_key
        sec, key = source_key(source)
        if not getattr(getattr(self.cfg, sec, None), key, True):
            return            # source switched off at runtime (e.g. by the Controller)
        if creator:
            self.desk.creators.setdefault(mint, creator)
        w = self.watch.get(mint)
        if w:
            if urgent:  # e.g. migration to an AMM -> re-check immediately
                w.last_sent = 0.0
                w.cand.source = source
                if "migration" in source:
                    # A graduation is a new life for the token: give it a full watch window from here, or the
                    # post-graduation wait would run out the window of a token discovered hours before.
                    w.added = time.time()
            return
        arch = self.archive.pop(mint, None)
        if len(self.watch) >= self.c.max_watchlist:
            self._prune(force=True)
            if len(self.watch) >= self.c.max_watchlist:
                return
        if arch is not None:     # a feed brought back a token we had archived: keep its history
            arch.cand.source = source
            self.watch[mint] = Watch(arch.cand, added=time.time(), max_liq=arch.max_liq,
                                     first_seen=arch.first_seen, revived=arch.revived)
            return
        t = time.time()
        self.watch[mint] = Watch(Candidate(mint, symbol or "?", name, source, creator=creator, pair_address=pair),
                                 added=t, first_seen=t)
        self.state.bump("discovered")
        self.state.bump(f"src:{source.split(':')[0]}")

    # ── loops ──
    async def run(self) -> None:
        self.status("working")
        tasks = [self.supervised("recheck", self.recheck_loop)]
        if self.c.revisit:
            self._seed_archive()
            tasks.append(self.supervised("second-look", self.revisit_loop))
        if self.c.pumpportal:
            tasks.append(self.supervised("pumpportal", self.pumpportal_loop))
        if self.c.geckoterminal:
            tasks.append(self.supervised("geckoterminal", self.gecko_loop))
        if self.c.dexscreener:
            tasks.append(self.supervised("dexscreener", self.dexscreener_loop))
        if self.desk.gmgn is not None and self.cfg.gmgn.signal_feed:
            tasks.append(self.supervised("gmgn-signals", self.gmgn_loop))
        self.emit("info", f"Scout online: venues={self.c.venues} sources pumpportal={self.c.pumpportal} "
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
                pools += await gecko_new_pools(self.desk.http, pages=1, dexes=venue_dexes(self.c.venues))
            except Exception:  # noqa: BLE001 - a flaky feed must not stop discovery
                failed += 1
            if n % 4 == 1:
                # Every ~80s: trending + top-volume pools. Tokens that are already moving are the ones that can
                # pass the $50k/h volume rule; new_pools alone mostly finds dust that never gets there.
                cutoff = time.time() - 48 * 3600
                try:
                    pools += [p for p in await gecko_active_pools(
                        self.desk.http, venue_dexes(self.c.venues) if self.c.venues == "pumpfun" else None)
                              if p.get("created") and _iso(p["created"]) > cutoff
                              and p.get("volume_h1", 0) >= self.cfg.filter.young_min_volume_usd]
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
            why = ""
            if mint in self.desk.blacklist or mint in held:
                why = "gone"                    # killed for good / bought: never archived
            elif age_min > self.c.watch_minutes:
                why = "window over"
            elif seen is None and age_min > 15:
                why = "no market"               # never got a market
            elif seen and seen[1] < 1_000 and age_min > 20:
                why = "dormant"                 # dormant, <$1k/h after 20 min
            elif force and seen and seen[1] < 10_000:
                why = "watchlist full"
            if why:
                self.watch.pop(mint, None)
                self.state.bump("dropped")
                if why != "gone":
                    self._archive(mint, w, why, t)

    # ── second look ──
    def _archive(self, mint: str, w: Watch, why: str, t: float) -> None:
        """Keep a dropped token for the second look, if it ever had a real market (or graduated): the thousands of
        pump.fun launches that never got past dust are not worth a DexScreener call every few minutes."""
        if not self.c.revisit or mint in self.desk.blacklist:
            return
        first = w.first_seen or w.added
        if t - first > self.c.revisit_hours * 3600:
            return
        seen = self.desk.market_seen.get(mint)
        peak = max(w.max_liq, seen[2] if seen else 0.0)
        if peak < 5_000 and "migration" not in (w.cand.source or ""):
            return
        self.archive[mint] = Archived(w.cand, first, t, why, peak, w.revived)
        if len(self.archive) > self.c.revisit_max:
            oldest = min(self.archive, key=lambda k: self.archive[k].dropped)
            self.archive.pop(oldest, None)
        self.state.counters["archived"] = len(self.archive)

    def _seed_archive(self) -> None:
        """After a restart the archive is empty: refill it with the tokens that reached the Filter's deep checks or
        the Analyst in the last revisit_hours and were never killed. Permanent kills are re-applied by the Filter
        anyway (rugged names, rug ledger, authorities, holder-spread record)."""
        try:
            since = time.time() - self.c.revisit_hours * 3600
            rows = self.desk.store.q(
                "SELECT mint, MAX(symbol) AS symbol, MIN(ts) AS first FROM decisions WHERE ts>=? "
                "AND (stage!='filter' OR outcome='pass') AND mint NOT IN "
                "(SELECT mint FROM decisions WHERE outcome='kill' AND ts>=?) GROUP BY mint "
                "ORDER BY first DESC LIMIT ?", (since, since, self.c.revisit_max))
        except Exception:  # noqa: BLE001 - a fresh database, or a store without decisions: start empty
            return
        t = time.time()
        for r in rows:
            mint = r["mint"]
            if not mint or mint in self.watch or mint in self.desk.blacklist:
                continue
            self.archive[mint] = Archived(Candidate(mint, r["symbol"] or "?", "", "revisit:seed"),
                                          first_seen=float(r["first"]), dropped=t, why="restart", max_liq=0.0)
        self.state.counters["archived"] = len(self.archive)

    def revive_due(self, snaps: dict, t: float | None = None) -> list[str]:
        """Put back on the watchlist every archived token that trades again. Returns the revived mints."""
        t = t or time.time()
        out = []
        for mint, a in list(self.archive.items()):
            if mint in self.desk.blacklist or mint in self.desk.portfolio.positions \
                    or t - a.first_seen > self.c.revisit_hours * 3600:
                self.archive.pop(mint, None)
                continue
            s = snaps.get(mint)
            if s is None:
                continue
            self.desk.market_seen[mint] = (t, s.volume_h1, s.liquidity_usd)
            if s.volume_h1 < self.c.revisit_min_volume_usd or s.liquidity_usd < self.c.revisit_min_liquidity_usd:
                continue
            if mint in self.watch or len(self.watch) >= self.c.max_watchlist:
                continue
            self.archive.pop(mint, None)
            a.cand.symbol = s.symbol or a.cand.symbol
            if not a.cand.source.startswith("revisit"):
                a.cand.source = "revisit:" + (a.cand.source or "?")
            self.watch[mint] = Watch(a.cand, added=t, max_liq=max(a.max_liq, s.liquidity_usd),
                                     first_seen=a.first_seen, revived=a.revived + 1)
            out.append(mint)
            self.state.bump("revived")
            self.emit("info", f"second look: back on the watchlist ({a.why} {((t - a.dropped) / 60):.0f} min ago, "
                              f"now ${s.volume_h1:,.0f}/h volume, ${s.liquidity_usd:,.0f} liquidity, "
                              f"{(t - a.first_seen) / 3600:.1f} h after discovery)", mint, a.cand.symbol)
        self.state.counters["archived"] = len(self.archive)
        return out

    async def revisit_loop(self) -> None:
        """Every revisit_seconds: one batched DexScreener read of the archive (30 mints per call, own small rate lane)."""
        while not self.desk.stop.is_set():
            if not await self.sleep(self.c.revisit_seconds):
                return
            if not self.archive:
                continue
            try:
                snaps = await self.desk.dex.snapshots(list(self.archive), lane="revisit")
            except Exception:  # noqa: BLE001 - try again on the next sweep
                self.state.bump("feed_errors")
                continue
            self.revive_due(snaps)

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
        rugnames.record(self.desk.store, mint, w.cand.symbol, w.cand.name, "liquidity pulled: " + reason)
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
