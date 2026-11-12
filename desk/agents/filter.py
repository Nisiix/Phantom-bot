"""FILTER — kills anything that fails a single safety rule.

Mandatory kills (user rules):
  1. mint authority not revoked                   (verified on-chain + RugCheck)
  2. LP not locked/burned (>= min_lp_locked_pct)  (liquidity-weighted over lockable pools; concentrated
                                                    DLMM/CLMM pools have no LP token and are capped instead;
                                                    LP locks and pool owners re-read on-chain, desk/onchain_lp.py)
  3. top-10 holders > 20%                          (pools / bonding curve excluded)
  4. last-hour volume < $50k                       (all pairs; tokens younger than 4h: in proportion to
                                                    their liquidity and age, see volume_floor)
  5. dev wallet connected to previous rugs         (RugCheck creator history, our own
                                                    rug ledger, serial dead-launch pattern)
Extra capital-preservation kills (config): freeze authority, dangerous Token-2022
extensions, danger-level RugCheck risks, minimum liquidity.

Instant-rug guards (2026-11-10, after 16 of 32 paper trades lost 60-100% within seconds):
  * graduation wait: no buy while the PumpSwap pair is younger than graduation_wait_minutes
    (rug rate measured: 52% under 30 min after graduation, 0 of 9 after 60 min);
  * holder spread: top10 seen above spread_top10_high_pct in the last hour and now under max_top10_pct means the
    supply was moved to fake wallets right before the dump -> permanent kill;
  * rugged names: a token named like one that rugged recently is held back unless its identity proves it is a
    different project (desk/identity.py: own website showing its contract, own X account, DexScreener profile,
    no channel borrowed from the rugged token); the same dev wallet as the rugged token -> permanent kill.

Optional GMGN layer (GMGN_API_KEY): honeypot flag, GMGN rug label, insider / bundler / sniper
shares, wash trading, serial-launcher dev, dev's SOL funder in our rug ledger, and holder
funding clusters (wallets funded by the same wallet = one hidden whale). GMGN outages never
let a token through on missing data: a check that can't be run is simply not added, the
mandatory checks above still apply.

Only a report where EVERY check passed is signed and handed to the Analyst.
Every rejection is logged with its reasons. A token that keeps failing for the
same reasons is logged again only when its reasons change (no log spam).
"""
from __future__ import annotations

import asyncio
import time

from ..clients.feeds import is_pump_copycat, is_pump_token
from ..clients.gmgn import GmgnError, dev_history, funding_clusters, summarize
from ..devwatch import linked_wallets
from ..funding import Funding
from ..onchain_lp import OnchainLP
from ..pools import quote_ok
from ..models import Candidate, Check, FilterPass, FilterReport, MarketSnapshot, now
from ..research import market_features, rug_features
from .. import identity
from ..rugnames import RuggedNames, seed_from_trades
from .base import Agent

OVERLAP_RISK_WORDS = ("lp", "liquidity", "holder", "mint authority", "freeze authority", "rug",
                      "ownership", "insider")
BONDING_CURVE_MARKETS = {"pump_fun", "pumpfun", "raydium_launchlab", "launchlab", "meteora_dbc", "letsbonk"}


# Concentrated-liquidity / position-NFT pools (Meteora DLMM & DAMM v2, Raydium CLMM, Orca whirlpools) have no
# fungible LP token, so RugCheck reports them as 0% locked even though there is nothing to burn. They are
# usually third-party market-maker liquidity next to the launch pool, not the dev's LP.
UNLOCKABLE_MARKETS = ("dlmm", "clmm", "whirlpool", "orca", "damm_v2", "dammv2")


def _is_unlockable(market_type: str) -> bool:
    return any(k in market_type for k in UNLOCKABLE_MARKETS)


def lp_lock_breakdown(report: dict, onchain: dict | None = None) -> dict:
    """LP lock split between pools that CAN be locked/burned and position-style pools that can't.

    locked_pct     liquidity-weighted lock % over lockable pools (bonding curves are program-held => locked;
                   a concentrated pool that RugCheck does report as locked counts as lockable)
    unlockable_pct share of total liquidity sitting in concentrated pools that are not verified third-party
    all_pct        the plain lock % over every pool (old metric)
    dev_pools      concentrated pools where the dev / funder / a top holder / a known rugger provides liquidity
    onchain        pools whose numbers came from the chain (see desk/onchain_lp.py) instead of RugCheck
    """
    onchain = onchain or {}
    total = lock_total = locked = unl = all_locked = 0.0
    kinds, dev_pools, n_chain = [], [], 0
    for m in report.get("markets") or []:
        mt = (m.get("marketType") or "").lower()
        lp = m.get("lp") or {}
        usd = float(lp.get("quoteUSD") or 0) + float(lp.get("baseUSD") or 0)
        if usd <= 0:
            continue
        oc = onchain.get(m.get("pubkey") or "") or {}
        pct = float(lp.get("lpLockedPct") or 0)
        if mt in BONDING_CURVE_MARKETS:
            pct = 100.0
        elif "locked_pct" in oc:
            pct, n_chain = oc["locked_pct"], n_chain + 1
        total += usd
        all_locked += usd * pct / 100
        if _is_unlockable(mt) and pct <= 0:
            status = oc.get("status")
            if status == "dev":
                dev_pools.append(oc.get("detail", mt))
                unl += usd
                kinds.append(f"{mt}:DEV")
            elif status == "clean":
                n_chain += 1
                kinds.append(f"{mt}:3rd-party")
            else:
                unl += usd
                kinds.append(f"{mt}:n/a")
        else:
            lock_total += usd
            locked += usd * pct / 100
            kinds.append(f"{mt}:{pct:.0f}%" + ("*" if "locked_pct" in oc else ""))
    return {
        "locked_pct": locked / lock_total * 100 if lock_total > 0 else None,
        "unlockable_pct": unl / total * 100 if total > 0 else 0.0,
        "all_pct": all_locked / total * 100 if total > 0 else None,
        "detail": ", ".join(kinds[:5]) if kinds else "no LP market found",
        "dev_pools": dev_pools,
        "onchain": n_chain,
    }


def lp_locked_pct(report: dict) -> tuple[float | None, str]:
    """Liquidity-weighted LP locked % over EVERY pool (strict metric). Bonding curves are program-held => locked."""
    b = lp_lock_breakdown(report)
    return b["all_pct"], b["detail"]


def pool_owner_set(report: dict) -> set[str]:
    owners: set[str] = set()
    for addr, info in (report.get("knownAccounts") or {}).items():
        if (info or {}).get("type") in ("AMM", "LOCKER", "BURN", "PROGRAM"):
            owners.add(addr)
    for m in report.get("markets") or []:
        owners.add(m.get("pubkey") or "")
        for k in ("liquidityAAccount", "liquidityBAccount"):
            o = (m.get(k) or {}).get("owner")
            if o:
                owners.add(o)
        for k in ("liquidityA", "liquidityB"):
            if m.get(k):
                owners.add(m[k])
    owners.discard("")
    return owners


def top10_pct(report: dict) -> tuple[float | None, int]:
    holders = report.get("topHolders") or []
    if not holders:
        return None, 0
    pools = pool_owner_set(report)
    real = [h for h in holders if h.get("owner") not in pools and h.get("address") not in pools]
    return sum(float(h.get("pct") or 0) for h in real[:10]), len(real)


def insider_pct(report: dict) -> float:
    supply = float((report.get("token") or {}).get("supply") or 0)
    if supply <= 0:
        return 0.0
    held = sum(float(n.get("currentHolding") or 0) for n in report.get("insiderNetworks") or [])
    return min(100.0, held / supply * 100)


def deep_priority(cand: Candidate, snap: MarketSnapshot | None) -> float:
    """Lower = sooner. Graduations / smart-money signals first, then the most traded tokens."""
    urgent = cand.source.endswith(":migration") or cand.source.startswith("gmgn:")
    vol = snap.volume_h1 if snap else 0.0
    return (0.0 if urgent else 1.0) - min(vol, 5e6) / 1e7


class Filter(Agent):
    name = "Filter"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.signer = desk.authority.signer("filter")
        self.f = self.cfg.filter
        self._last_reason: dict[str, str] = {}
        self._top10_seen: dict[str, float] = {}
        self._wait_hist: dict[str, list[tuple]] = {}   # mint -> readings during the graduation wait
        self._released: set[str] = set()                # graduation wait lifted early (re-checked every time)
        self._last_pass_log: dict[str, float] = {}
        self._deep: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._deep_pending: set[str] = set()
        self._seq = 0
        rpc_url = self.cfg.secrets.rpc_url or ""
        self.rugged = RuggedNames(desk.store, self.f.rugged_name_days)
        self.site_probe = identity.SiteProbe()
        self._links_seen: set[str] = set()
        try:
            seed_from_trades(desk.store, self.f.rugged_name_loss_pct)
        except Exception:  # noqa: BLE001 - the ledger also fills from live observations
            pass
        # mint -> [ts, top10 %] of the last reading above spread_top10_high_pct (survives restarts)
        self._top10_high: dict[str, list] = desk.store.kv_get("filter_top10_high", {}) or {}
        self.onchain = OnchainLP(desk.rpc, private_rpc="api.mainnet-beta.solana.com" not in rpc_url)
        self.funding = Funding(desk.store, desk.rpc, int(self.f.funder_hops), int(self.f.funder_hub_signatures))

    async def run(self) -> None:
        self.status("idle")
        n = max(1, int(self.f.deep_workers))
        self.emit("info", f"Filter online: mint/LP/top10/volume/dev-rug kills armed ({n} deep-check workers)")
        await asyncio.gather(self.supervised("filter-screen", self.loop),
                             *(self.supervised(f"filter-deep-{i}", self.deep_loop) for i in range(n)),
                             self.supervised("filter-funders", self.funder_backfill))

    async def funder_backfill(self) -> None:
        """Look up who funded the wallets already in the rug ledger (slowly, public RPC), so a new creator funded
        by the same wallet as an old rugger is caught (desk/funding.py)."""
        every = float(self.f.funder_backfill_seconds)
        if not self.f.funder_check or every <= 0 or self.desk.rpc is None:
            return
        while not self.desk.stop.is_set():
            todo = self.desk.store.ledger_without_funder(20)
            if not todo:
                await asyncio.sleep(300)
                continue
            for w in todo:
                if self.desk.stop.is_set():
                    return
                try:
                    await self.funding.funder(w)
                    self.state.bump("funders_backfilled")
                except Exception:  # noqa: BLE001 - RPC busy: try again later
                    self.state.bump("funder_errors")
                    await asyncio.sleep(30)
                await asyncio.sleep(every)

    async def loop(self) -> None:
        """Stage 1 — cheap market screen, batched (30 mints per DexScreener call).

        Survivors go to a priority queue for the slow on-chain/RugCheck/GMGN checks, so a backlog
        of deep checks never stalls the screen (and the screen never stalls deep checks)."""
        q = self.desk.candidates
        while not self.desk.stop.is_set():
            try:
                first = await asyncio.wait_for(q.get(), timeout=2)
            except asyncio.TimeoutError:
                if not self._deep_pending:
                    self.status("idle")
                continue
            batch = [first]
            while len(batch) < 30 and not q.empty():
                batch.append(q.get_nowait())
            batch = list({c.mint: c for c in batch
                          if c.mint not in self.desk.blacklist and c.mint not in self._deep_pending}.values())
            if not batch:
                continue
            self.status("working")
            snaps = await self.desk.dex.snapshots([c.mint for c in batch])
            for cand in batch:
                snap = snaps.get(cand.mint)
                report = self.screen(cand, snap)
                if report is not None:
                    await self.route(cand, report)
                    continue
                self._deep_pending.add(cand.mint)
                self._seq += 1
                self._deep.put_nowait((deep_priority(cand, snap), self._seq, time.time(), cand, snap))
            self.state.counters["deep_queue"] = self._deep.qsize()

    async def deep_loop(self) -> None:
        """Stage 2 — deep checks, several in flight (RPC / GMGN latency overlaps; RugCheck keeps its own limit)."""
        while not self.desk.stop.is_set():
            try:
                _, _, queued, cand, snap = await asyncio.wait_for(self._deep.get(), timeout=2)
            except asyncio.TimeoutError:
                continue
            try:
                if cand.mint in self.desk.blacklist:
                    continue
                if time.time() - queued > self.f.deep_max_wait_seconds:
                    self.state.bump("deep_stale")   # market data too old: the Scout will resubmit it
                    continue
                self.status("working")
                try:
                    report = await self.deep(cand, snap)
                except Exception as e:  # noqa: BLE001
                    self.emit("error", f"evaluation failed: {e}", cand.mint, cand.symbol)
                    continue
                await self.route(cand, report)
            finally:
                self._deep_pending.discard(cand.mint)
                self.state.counters["deep_queue"] = self._deep.qsize()

    # ── core evaluation ──
    async def evaluate(self, cand: Candidate, snap: MarketSnapshot | None) -> FilterReport:
        """Full evaluation in one call (screen + deep checks)."""
        return self.screen(cand, snap) or await self.deep(cand, snap)

    def screen(self, cand: Candidate, snap: MarketSnapshot | None) -> FilterReport | None:
        """Cheap market checks. Returns a failing report, or None if the token deserves deep checks."""
        self.state.bump("evaluated")
        sym = snap.symbol if snap else cand.symbol
        if snap is None:
            return FilterReport(cand.mint, sym, False, [Check("market", False, "none", "listed",
                                                               "no DEX pair indexed yet")])
        self.desk.market_seen[cand.mint] = (time.time(), snap.volume_h1, snap.liquidity_usd)
        checks = self._market_checks(snap)
        self._note_links(cand.mint, snap)
        hit = self.rugged.match(cand.mint, snap.symbol or cand.symbol, snap.name or cand.name)
        name_check = None
        if hit:
            same_dev = bool(cand.creator and hit.get("creator") and cand.creator == hit["creator"])
            if same_dev or not self.f.rugged_name_identity:
                checks.append(Check("rugged_name", False, hit["symbol"] or hit["key"], "new name",
                                    f"relaunch of a name that rugged: {hit['symbol'] or hit['key']} "
                                    f"({str(hit['mint'])[:6]}…, {hit['reason'][:60]})"
                                    + (f", same dev {cand.creator[:6]}" if same_dev else ""), permanent=True))
            else:
                # Same name, maybe a different project: the deep stage checks its identity (desk/identity.py).
                # Listed here only if the token fails something else anyway, and never as a kill: the real
                # team often pays its DexScreener profile / publishes the site later, so it is re-checked.
                name_check = Check("rugged_name", False, hit["symbol"] or hit["key"], "different project",
                                   f"same name as rugged {hit['symbol'] or hit['key']} ({str(hit['mint'])[:6]}…, "
                                   f"{hit['reason'][:60]}); identity not checked yet")
        if self.cfg.scout.venues == "pumpfun":
            # pump.fun-only desk: tokens born elsewhere (Raydium LaunchLab, Meteora, ...) that reach us through
            # DexScreener profiles never get RugCheck / RPC time. Not permanent: it is a scope choice, not a risk.
            ok = is_pump_token(cand.mint, snap.dex_id)
            if ok:
                detail = "pump.fun token"
            elif is_pump_copycat(cand.mint, snap.dex_id):
                # Pool opened straight on PumpSwap for a mint pump.fun never created: copycat / honeypot pattern.
                detail = f"copycat: mint not created by pump.fun (no 'pump' suffix) but pooled on {snap.dex_id}"
            else:
                detail = f"not a pump.fun token (main pair on {snap.dex_id or '?'})"
            checks.append(Check("venue", ok, snap.dex_id or "?", "pump.fun / PumpSwap", detail))
            if not ok:   # out of scope for good: the Scout drops it, no more re-screens (quietly, not a "kill")
                self.desk.blacklist[cand.mint] = detail
        old = next((c for c in checks if c.name == "token_age" and not c.passed), None)
        if old is not None:   # a token only gets older: out of scope for good, no more re-screens
            self.desk.blacklist.setdefault(cand.mint, old.detail)
        failed = [c for c in checks if not c.passed]
        if failed and [c.name for c in failed] == ["graduation_wait"] and (
                not self._recent_top10(cand.mint) or cand.mint in self._released):
            # Only the graduation wait fails: still run the deep checks every few minutes, so the holder-spread
            # guard sees the top10 while the supply is still concentrated (it moves to fake wallets later).
            return None
        if failed and name_check is not None:
            checks.append(name_check)
        if failed:
            # Cheap market checks failed: stop here, don't burn RugCheck quota.
            return FilterReport(cand.mint, sym, False, checks, market=snap)
        return None

    def _recent_top10(self, mint: str) -> bool:
        """A deep check (with a top10 reading) ran for this token in the last 5 minutes."""
        return time.time() - self._top10_seen.get(mint, 0) < 300

    def graduation_age(self, snap: MarketSnapshot) -> float | None:
        """Minutes since the pump.fun -> PumpSwap graduation (the PumpSwap pair's creation), None if unknown."""
        if (snap.dex_id or "").lower() != "pumpswap" or not snap.pair_created_at:
            return None
        return snap.age_minutes

    def volume_floor(self, snap: MarketSnapshot) -> float:
        """$50k/h for a token older than [analyst] young_age_hours (it may have pumped already: full bar).
        A younger token is judged against its own pool: young_volume_liquidity_ratio x liquidity (at most $50k),
        at the same pace in its first hour (it can't have a full hour of volume yet), never under
        young_min_volume_usd. Waiting for $50k/h on a fresh token means buying after the pump."""
        f = self.f
        age = snap.age_minutes if snap.pair_created_at else None
        if age is None or age >= self.cfg.analyst.young_age_hours * 60:
            return f.min_volume_h1_usd
        base = min(f.min_volume_h1_usd, f.young_volume_liquidity_ratio * snap.liquidity_usd)
        pace = min(1.0, max(0.25, age / 60))
        return max(f.young_min_volume_usd, base * pace)

    def _market_checks(self, snap: MarketSnapshot) -> list[Check]:
        f = self.f
        floor = self.volume_floor(snap)
        young = floor < f.min_volume_h1_usd
        label = f"${floor:,.0f}" + (f" (young token: {snap.age_minutes:.0f} min old, ${snap.liquidity_usd:,.0f} "
                                    f"liquidity)" if young else "")
        checks = [
            Check("volume_1h", snap.volume_h1 >= floor, round(snap.volume_h1), round(floor),
                  f"${snap.volume_h1:,.0f}/h < {label}" if snap.volume_h1 < floor
                  else f"${snap.volume_h1:,.0f}/h" + (f" >= {label}" if young else "")),
            Check("liquidity", snap.liquidity_usd >= f.min_liquidity_usd, round(snap.liquidity_usd),
                  f.min_liquidity_usd, f"${snap.liquidity_usd:,.0f}"),
        ]
        if snap.quote_mint and not quote_ok(snap.quote_mint, list(f.allowed_quote_mints)):
            # BERNIE (2026-11-11): its only real pool traded against SPCX, another memecoin, next to dust SOL
            # copies with made-up prices. Not permanent: a SOL / USDC pool may become the main one later.
            checks.append(Check("quote_currency", False, snap.quote_symbol or snap.quote_mint[:6], "SOL",
                                f"main pool trades against {snap.quote_symbol or snap.quote_mint[:6]}, not SOL "
                                f"(the desk buys only in SOL pools)"))
        grad = self.graduation_age(snap)
        if f.graduation_wait_minutes > 0 and grad is not None:
            ok = grad >= f.graduation_wait_minutes
            checks.append(Check("graduation_wait", ok, round(grad, 1), f.graduation_wait_minutes,
                                f"graduated {grad:.0f} min ago" + ("" if ok else
                                f" < {f.graduation_wait_minutes:.0f} min wait (instant-rug window)")))
        # New tokens only (user's rule 2026-11-11): the desk hunts fresh launches that can still grow to about
        # $200k market cap or more; a token that is days old, or already far up there, had its first run.
        # Age = since the token's oldest DEX pair (unknown age / market cap: not judged).
        if f.max_age_hours > 0 and snap.pair_created_at:
            age_h = snap.age_minutes / 60
            ok = age_h <= f.max_age_hours
            checks.append(Check("token_age", ok, round(age_h, 1), f.max_age_hours,
                                f"{age_h / 24:.1f} days old" + ("" if ok else
                                f" > {f.max_age_hours / 24:g} days: not a new token")))
        mcap = snap.market_cap or snap.fdv
        if f.max_market_cap_usd > 0 and mcap > 0:
            ok = mcap <= f.max_market_cap_usd
            checks.append(Check("market_cap", ok, round(mcap), f.max_market_cap_usd,
                                f"market cap ${mcap:,.0f}" + ("" if ok else
                                f" > ${f.max_market_cap_usd:,.0f}: already ran, the first move is gone")))
        return checks

    async def deep(self, cand: Candidate, snap: MarketSnapshot) -> FilterReport:
        """On-chain / RugCheck / GMGN checks for a token that passed the market screen."""
        f = self.f
        sym = snap.symbol
        checks = self._market_checks(snap)
        rc = await self.desk.rug.report(cand.mint)
        if not rc:
            checks.append(Check("rugcheck", False, "unavailable", "report", "RugCheck report unavailable"))
            return FilterReport(cand.mint, sym, False, checks, market=snap)

        token = rc.get("token") or {}
        creator = rc.get("creator") or self.desk.creators.get(cand.mint)
        if creator:
            self.desk.creators.setdefault(cand.mint, creator)

        # 1. Mint authority (on-chain truth wins; RugCheck as fallback)
        mint_auth, freeze_auth, src = token.get("mintAuthority"), token.get("freezeAuthority"), "rugcheck"
        try:
            info = await self.desk.rpc.mint_info(cand.mint)
            if info is not None:
                mint_auth, freeze_auth, src = info.get("mintAuthority"), info.get("freezeAuthority"), "on-chain"
        except Exception as e:  # noqa: BLE001
            self.emit("warn", f"RPC mint check failed, using RugCheck: {e}", cand.mint, sym)
        checks.append(Check("mint_revoked", mint_auth is None, mint_auth or "revoked", "revoked",
                            f"mint authority {'ACTIVE ' + str(mint_auth)[:8] if mint_auth else 'revoked'} ({src})",
                            permanent=mint_auth is not None))
        if f.require_freeze_revoked:
            checks.append(Check("freeze_revoked", freeze_auth is None, freeze_auth or "revoked", "revoked",
                                f"freeze authority {'ACTIVE' if freeze_auth else 'revoked'} ({src})",
                                permanent=freeze_auth is not None))

        # 2. LP locked. Lockable pools (launch AMM / CPMM / bonding curve) must be >= min_lp_locked_pct locked.
        # Concentrated-liquidity pools can't be locked at all: they are left out of that %, but their share of
        # total liquidity is capped (max_unlockable_liquidity_pct; 0 = old strict rule, they count as unlocked).
        onchain = await self._onchain_lp(rc, creator, cand.mint) if f.onchain_lp_check else {}
        lpb = lp_lock_breakdown(rc, onchain)
        max_unl = f.max_unlockable_liquidity_pct
        if max_unl <= 0:
            lp_pct, lp_ok = lpb["all_pct"], lpb["all_pct"] is not None and lpb["all_pct"] >= f.min_lp_locked_pct
            lp_text = (f"{lp_pct:.1f}% locked [{lpb['detail']}]" if lp_pct is not None else lpb["detail"])
        else:
            lp_pct, unl = lpb["locked_pct"], lpb["unlockable_pct"]
            lp_ok = lp_pct is not None and lp_pct >= f.min_lp_locked_pct and unl <= max_unl
            if lp_pct is None:
                lp_text = "no lockable LP pool: " + lpb["detail"]
            else:
                lp_text = f"{lp_pct:.1f}% locked [{lpb['detail']}]"
                if unl >= 0.5:
                    lp_text += (f", {unl:.0f}% of liquidity in unverified concentrated pools"
                                + (f" > max {max_unl:.0f}%" if unl > max_unl else ""))
        if lpb["dev_pools"]:
            lp_ok = False
            lp_text = "dev-linked liquidity: " + "; ".join(lpb["dev_pools"][:2]) + " | " + lp_text
        if lpb["onchain"]:
            lp_text += " (on-chain)"
        checks.append(Check("lp_locked", lp_ok, None if lp_pct is None else round(lp_pct, 1),
                            f.min_lp_locked_pct, lp_text))

        # 3. Top 10 holders
        t10, n_holders = top10_pct(rc)
        checks.append(Check("top10_holders", t10 is not None and t10 <= f.max_top10_pct,
                            None if t10 is None else round(t10, 2), f.max_top10_pct,
                            f"top10 hold {t10:.1f}% (pools excluded)" if t10 is not None else "holder data missing"))
        spread = self._holder_spread(cand.mint, t10)
        if spread:
            checks.append(Check("holder_spread", False, round(t10, 1), f"top10 < {f.spread_top10_high_pct:.0f}% "
                                f"for {f.spread_window_minutes:.0f} min", spread, permanent=True))

        # 5. Dev wallet connected to previous rugs
        rug_reasons = []
        if rc.get("rugged"):
            rug_reasons.append("RugCheck marks this token rugged")
        for r in rc.get("risks") or []:
            nm = (r.get("name") or "").lower()
            if "rug" in nm:
                rug_reasons.append(r.get("name"))
        known = self.desk.store.rugger(creator)
        if known:
            rug_reasons.append(f"dev in rug ledger ({known['reason'][:60]})")
        prior = [t for t in (rc.get("creatorTokens") or []) if t.get("mint") != cand.mint]
        dead = [t for t in prior if float(t.get("marketCap") or 0) < 10_000]
        # serial_dev_min_launches = 0: off (user 2026-11-12: serial ruggers use a fresh wallet every time)
        if (f.serial_dev_min_launches > 0 and len(prior) >= f.serial_dev_min_launches
                and len(dead) / len(prior) >= f.serial_dev_dead_ratio):
            rug_reasons.append(f"serial dev: {len(dead)}/{len(prior)} prior launches dead")
        checks.append(Check("dev_rug_link", not rug_reasons, "; ".join(rug_reasons) or "clean", "clean",
                            "; ".join(rug_reasons) or f"dev {str(creator)[:6]} clean ({len(prior)} prior)",
                            permanent=bool(rug_reasons)))
        # 5b. Who funded the creator (user 2026-11-12): a fresh wallet paid by a rugger, or by a wallet that paid
        # another rugging creator, is the same operator (desk/funding.py).
        if creator and f.funder_check and not rug_reasons:
            try:
                why, chain = await self.funding.check(creator)
            except Exception as e:  # noqa: BLE001 - RPC down: not judged, retried on the next check
                why, chain = None, []
                self.state.bump("funder_errors")
                checks.append(Check("funder_rug_link", True, "n/a", "clean",
                                    f"funder not read ({e.__class__.__name__}): retried next check"))
            else:
                first = next((r["funder"] for r in chain if r.get("funder") and not r.get("hub")), None)
                if first:
                    self.desk.funders.setdefault(cand.mint, first)
                desc = " <- ".join(f"{(r.get('funder') or '?')[:6]}{' (exchange)' if r.get('hub') else ''}"
                                   for r in chain) or "unknown"
                checks.append(Check("funder_rug_link", why is None, why or "clean", "clean",
                                    why or f"funded by {desc}", permanent=why is not None))
                if why:
                    rug_reasons.append(why)
        if rug_reasons and creator:
            self.desk.store.add_rugger(creator, cand.mint, rug_reasons[0][:120])

        # 6. Same name as a rugged token: relaunch / copycat, or a different (real) project?
        ident = None
        hit = self.rugged.match(cand.mint, snap.symbol or cand.symbol, snap.name or cand.name)
        if hit:
            ident = await self._identity(cand.mint, creator, hit, snap, (rc.get("tokenMeta") or {}).get("uri"))
            checks.append(Check("rugged_name", ident["ok"], hit["symbol"] or hit["key"], "different project",
                                ident["detail"], permanent=ident["permanent"]))

        # Extras
        if f.reject_dangerous_token2022:
            ext = rc.get("token_extensions") or {}
            bad = []
            if ext.get("permanentDelegate"):
                bad.append("permanent delegate")
            if ext.get("nonTransferable"):
                bad.append("non-transferable")
            if float((rc.get("transferFee") or {}).get("pct") or 0) > 0:
                bad.append(f"transfer fee {rc['transferFee']['pct']}%")
            das = ext.get("defaultAccountState")
            if das and "frozen" in str(das).lower():
                bad.append("accounts frozen by default")
            checks.append(Check("token2022_safe", not bad, ", ".join(bad) or "ok", "none",
                                ", ".join(bad) or "no dangerous extensions", permanent=bool(bad)))
        if f.reject_rugcheck_danger:
            danger = [r.get("name") for r in rc.get("risks") or [] if r.get("level") == "danger"
                      and not any(w in (r.get("name") or "").lower() for w in OVERLAP_RISK_WORDS)]
            checks.append(Check("rugcheck_danger", not danger, ", ".join(danger) or "none", "none",
                                ", ".join(danger) or "no danger-level risks"))

        self._early_release(cand.mint, snap, checks, t10, float(rc.get("totalHolders") or n_holders or 0),
                            (float(rc.get("creatorBalance") or 0) / float(token.get("supply") or 0) * 100
                             if rc.get("creatorBalance") is not None and float(token.get("supply") or 0) > 0
                             else None))

        gm = None
        if self.desk.gmgn is not None and all(c.passed for c in checks):
            gm = await self.gmgn_checks(cand.mint, sym, creator, checks)

        # 7. Creator and linked wallets selling (user's rule 2026-11-11): more than 10% of what they held from
        # the start -> dangerous, dropped for good (desk/devwatch.py).
        dev = await self._dev_sold(cand.mint, creator, rc, token, checks)
        if dev is not None:
            checks.append(dev)

        passed = all(c.passed for c in checks)
        rug_facts = {
            "top10_pct": t10, "holders_total": rc.get("totalHolders") or n_holders,
            "insider_pct": insider_pct(rc), "insider_networks": len(rc.get("insiderNetworks") or []),
            "lp_locked_pct": lp_pct, "lp_unlockable_pct": lpb["unlockable_pct"], "risks": [(r.get("name"), r.get("level")) for r in rc.get("risks") or []],
            "creator": creator, "creator_prior_launches": len(prior),
            "creator_pct": (float(rc.get("creatorBalance") or 0) / float(token.get("supply") or 0) * 100
                            if rc.get("creatorBalance") is not None and float(token.get("supply") or 0) > 0 else None),
            "mutable_metadata": bool((rc.get("tokenMeta") or {}).get("mutable")),
            "launchpad": (rc.get("launchpad") or {}).get("name"),
            "metadata_uri": (rc.get("tokenMeta") or {}).get("uri"),
            "decimals": token.get("decimals"),
            "gmgn": gm,
            "identity": ident and {k: ident[k] for k in ("ok", "score", "detail")},
        }
        return FilterReport(cand.mint, sym, passed, checks, market=snap, rug=rug_facts, creator=creator,
                            source=cand.source)

    async def _dev_sold(self, mint: str, creator: str | None, rc: dict, token: dict,
                        checks: list[Check]) -> Check | None:
        """Has the creator's group sold more than [devwatch] max_sold_pct of what it held from the start?
        The creator is read on-chain at every deep check (at most every filter_check_seconds); the linked
        wallets (RugCheck insider networks with the creator, GMGN funder) are added once the token passes
        everything else, to spare RugCheck's budget. A read that fails never kills: the next check retries."""
        dw, c = self.desk.devwatch, self.cfg.devwatch
        if dw is None or not c.enabled or any(not x.passed and x.permanent for x in checks):
            return None
        dec = int(token.get("decimals") or 0)
        supply = float(token.get("supply") or 0) / 10 ** dec
        g = dw.group(mint, creator, supply, rc.get("tokenProgram"))
        if g is None:
            return Check("dev_sold", True, "n/a", f"<= {c.max_sold_pct:g}%", "creator unknown: not judged")
        added = False
        if all(x.passed for x in checks):
            try:
                graph = await self.desk.rug.insider_graph(mint)
            except Exception:  # noqa: BLE001 - RugCheck down: the creator alone is still watched
                graph = []
            for w, role in linked_wallets(g.creator, graph, c.max_linked_wallets).items():
                added |= g.add(w, role)
            funder = self.desk.funders.get(mint)
            if funder:
                added |= g.add(funder, "dev funder")
        if added or time.time() - g.updated >= c.filter_check_seconds:
            try:
                await dw.refresh(g)
            except Exception as e:  # noqa: BLE001
                self.state.bump("devwatch_errors")
                if not g.updated:
                    return Check("dev_sold", True, "n/a", f"<= {c.max_sold_pct:g}%",
                                 f"creator balance unreadable ({e.__class__.__name__}): retried next check")
        if not g.judgeable(c.min_hold_pct):
            return Check("dev_sold", True, round(g.sold_pct, 1), f"<= {c.max_sold_pct:g}%",
                         f"creator group held under {c.min_hold_pct:g}% of supply (dust): not judged")
        bad = dw.breached(g)
        return Check("dev_sold", not bad, round(g.sold_pct, 1), f"<= {c.max_sold_pct:g}%",
                     ("DANGEROUS: " if bad else "") + g.describe(), permanent=bad)

    def _note_links(self, mint: str, snap: MarketSnapshot) -> None:
        """Remember which channels (X account, website, Telegram) each token claims, once per mint: a copycat
        that borrows the channels of a token that later rugs is recognised by them (desk/identity.py)."""
        if mint in self._links_seen or not (snap.websites or snap.socials):
            return
        self._links_seen.add(mint)
        if len(self._links_seen) > 50000:
            self._links_seen.clear()
        try:
            self.desk.store.add_token_links(mint, identity.link_keys(identity.channels(snap.websites, snap.socials)))
        except Exception:  # noqa: BLE001 - evidence only
            pass

    async def _identity(self, mint: str, creator: str | None, hit: dict, snap: MarketSnapshot,
                        metadata_uri: str | None) -> dict:
        if not self.f.rugged_name_identity:
            return {"ok": False, "permanent": True, "score": 0, "channels": {},
                    "detail": f"relaunch of a name that rugged: {hit['symbol'] or hit['key']} "
                              f"({str(hit['mint'])[:6]}…, {hit['reason'][:60]})"}
        try:
            return await asyncio.wait_for(
                identity.verify(self.desk.store, self.site_probe, mint, creator, hit, snap.websites, snap.socials,
                                metadata_uri, self.f.identity_min_score,
                                self.f.identity_tg_min_members), timeout=25)
        except Exception as e:  # noqa: BLE001 - unproven = held back, re-checked on the next pass
            return {"ok": False, "permanent": False, "score": 0, "channels": {},
                    "detail": f"same name as rugged {hit['symbol'] or hit['key']}; identity check failed "
                              f"({e.__class__.__name__})"}

    def _early_release(self, mint: str, snap: MarketSnapshot, checks: list[Check], t10: float | None,
                       holders: float, dev_pct: float | None) -> None:
        """Lift the graduation wait early when the token has proven steady since graduation (user request).

        Every deep check during the wait is recorded. The wait is lifted only if ALL hold:
          * at least early_release_min_minutes since graduation (instant rugs hit 5-26 min after it);
          * we watched it from the start: first reading within early_release_first_seen_minutes of graduation,
            at least early_release_min_readings readings spanning early_release_min_span_minutes;
          * top10 never above max_top10_pct in ANY reading (the rugs were 74-99% before spreading to fake wallets);
          * holders grew and liquidity held (>= 90% of the first reading); dev holds <= early_release_max_dev_pct;
          * every other check passes now."""
        f = self.f
        wait = next((c for c in checks if c.name == "graduation_wait"), None)
        grad = self.graduation_age(snap)
        if grad is None or wait is None:
            return
        hist = self._wait_hist.setdefault(mint, [])
        hist.append((time.time(), grad, t10, holders, snap.liquidity_usd))
        if len(self._wait_hist) > 5000:
            cutoff = time.time() - 2 * 3600
            self._wait_hist = {k: v for k, v in self._wait_hist.items() if v[-1][0] > cutoff}
        if wait.passed or not f.early_release:
            return
        if any(not c.passed for c in checks if c.name != "graduation_wait"):
            return
        first, last = hist[0], hist[-1]
        why = []
        if grad < f.early_release_min_minutes:
            why.append(f"only {grad:.0f} min since graduation")
        if first[1] > f.early_release_first_seen_minutes:
            why.append(f"first seen {first[1]:.0f} min after graduation")
        if len(hist) < f.early_release_min_readings or (last[0] - first[0]) / 60 < f.early_release_min_span_minutes:
            why.append(f"{len(hist)} readings over {(last[0] - first[0]) / 60:.0f} min")
        if any(h[2] is None or h[2] > f.max_top10_pct for h in hist):
            why.append("top10 was concentrated")
        if last[3] <= first[3]:
            why.append("holders not growing")
        if last[4] < 0.9 * first[4]:
            why.append("liquidity dropping")
        if dev_pct is not None and dev_pct > f.early_release_max_dev_pct:
            why.append(f"dev holds {dev_pct:.1f}%")
        if why:
            wait.detail += " | early release no: " + ", ".join(why)
            return
        wait.passed = True
        self._released.add(mint)
        wait.detail = (f"graduated {grad:.0f} min ago: EARLY RELEASE (steady since graduation: {len(hist)} readings, "
                       f"top10 <= {max(h[2] for h in hist):.1f}%, holders {first[3]:.0f} -> {last[3]:.0f}, "
                       f"liquidity ${first[4]:,.0f} -> ${last[4]:,.0f})")
        self.state.bump("early_release")

    def _holder_spread(self, mint: str, t10: float | None) -> str | None:
        """Remember high top10 readings; flag a token whose top10 went from > spread_top10_high_pct to under
        max_top10_pct within spread_window_minutes (seen on every instant rug: 74-99% -> 0.5-9% in ~10 min)."""
        if t10 is None:
            return None
        f, t = self.f, time.time()
        self._top10_seen[mint] = t
        if len(self._top10_seen) > 20000:
            self._top10_seen = {k: v for k, v in self._top10_seen.items() if t - v < 3600}
        window = f.spread_window_minutes * 60
        if t10 > f.spread_top10_high_pct:
            self._top10_high[mint] = [t, round(t10, 1)]
            self._top10_high = {k: v for k, v in self._top10_high.items() if t - v[0] < window}
            self.desk.store.kv_set("filter_top10_high", self._top10_high)
            return None
        high = self._top10_high.get(mint)
        if high and t - high[0] < window and t10 <= f.max_top10_pct:
            return (f"holders spread to fake wallets: top10 {high[1]:.0f}% {(t - high[0]) / 60:.0f} min ago, "
                    f"now {t10:.1f}%")
        return None

    async def _onchain_lp(self, rc: dict, creator: str | None, mint: str) -> dict:
        """Read LP locks and concentrated-pool position owners from the chain (desk/onchain_lp.py)."""
        suspicious: dict[str, str] = {}
        pools = pool_owner_set(rc)
        for h in (rc.get("topHolders") or [])[:12]:
            o = h.get("owner")
            if o and o not in pools and h.get("address") not in pools:
                suspicious[o] = "top holder"
        funder = self.desk.funders.get(mint)
        if funder:
            suspicious[funder] = "dev funder"
        if creator:
            suspicious[creator] = "dev"
        try:
            return await asyncio.wait_for(
                self.onchain.analyze(rc, suspicious, rugger=lambda w: self.desk.store.rugger(w) is not None),
                timeout=20)
        except Exception:  # noqa: BLE001 - chain unreachable: RugCheck numbers + the 50% cap still apply
            self.state.bump("onchain_lp_errors")
            return {}

    async def gmgn_checks(self, mint: str, sym: str, creator: str | None, checks: list[Check]) -> dict | None:
        g, gm = self.cfg.gmgn, self.desk.gmgn
        try:
            info, sec = await asyncio.gather(gm.token_info(mint), gm.token_security(mint))
        except GmgnError as e:
            self.emit("warn", f"GMGN unavailable ({e}); GMGN checks skipped", mint, sym)
            return None
        s = summarize(info, sec)
        if s["honeypot"] == 1 or s["can_not_sell"] in (1, True):
            checks.append(Check("gmgn_honeypot", False, "honeypot", "sellable", "GMGN: cannot sell", permanent=True))
        if s["rug_ratio"] is not None:
            checks.append(Check("gmgn_rug_label", s["rug_ratio"] <= g.max_rug_ratio, round(s["rug_ratio"], 2),
                                g.max_rug_ratio, f"GMGN rug ratio {s['rug_ratio']:.2f}",
                                permanent=s["rug_ratio"] > g.max_rug_ratio))
        for key, name, lim in (("insider_rate", "insiders", g.max_insider_rate),
                               ("bundler_rate", "bundlers", g.max_bundler_rate),
                               ("sniper_rate", "snipers", g.max_sniper_rate)):
            v = s[key]
            if v is not None:
                checks.append(Check(f"gmgn_{name}", v <= lim, round(v * 100, 1), lim * 100,
                                    f"{name} hold {v * 100:.1f}% (max {lim * 100:.0f}%)"))
        if s["is_wash_trading"] in (1, True):
            checks.append(Check("gmgn_wash_trading", False, True, False, "GMGN flags wash trading"))
        funder = s.get("dev_funder")
        if funder:
            self.desk.funders[mint] = funder
            hit = self.desk.store.rugger(funder) is not None
            checks.append(Check("dev_funder_rug_link", not hit, funder[:8], "clean",
                                f"dev funded by known rugger {funder[:6]}" if hit else "dev funder clean",
                                permanent=hit))
        creator = creator or s.get("creator")
        if creator:
            try:
                dh = dev_history(await gm.created_tokens(creator))
                s.update({f"dev_{k}": v for k, v in dh.items()})
                serial = (g.serial_min_launches > 0 and dh["launches"] >= g.serial_min_launches and dh["open_ratio"] is not None
                          and dh["open_ratio"] <= g.serial_max_graduation)
                checks.append(Check("dev_serial_launcher", not serial, dh["launches"], g.serial_min_launches,
                                    f"dev launched {dh['launches']}, graduated {dh['graduated']}",
                                    permanent=serial))
                if serial:
                    self.desk.store.add_rugger(creator, mint, f"serial launcher {dh['launches']} launches")
            except GmgnError:
                pass
        if all(c.passed for c in checks):
            try:
                fc = funding_clusters(await gm.top_holders(mint), mint)
                s.update(fc)
                checks.append(Check("funding_clusters", fc["cluster_pct"] <= g.max_funding_cluster_pct,
                                    fc["cluster_pct"], g.max_funding_cluster_pct,
                                    f"{fc['cluster_pct']:.1f}% of supply in {len(fc['clusters'])} same-funder "
                                    f"wallet clusters" + (" (scripted funding)" if fc["synced_funding"] else "")))
            except GmgnError:
                pass
        return s

    async def route(self, cand: Candidate, report: FilterReport) -> None:
        store = self.desk.store
        if self.desk.shadow is not None and report.market is not None:
            outcome = "pass" if report.passed else ("kill" if report.permanent_fail else "reject")
            self.desk.shadow.start(report.mint, report.symbol, "filter", outcome, report.reason_text(),
                                   report.market.price_usd, report.market.liquidity_usd, source=cand.source,
                                   features={**market_features(report.market, cand.source),
                                             **rug_features(report.rug)})
            # Held back only for now by the graduation wait: also follow it from the moment the wait ends,
            # to measure what the wait costs or saves (stage "grad45" in the shadow table).
            if not report.passed and not report.permanent_fail and any(
                    c.name == "graduation_wait" and not c.passed for c in report.checks):
                self.desk.shadow.defer_graduation(report.mint, report.symbol, report.market.pair_created_at,
                                                  self.f.graduation_wait_minutes, report.reason_text())
        report.source = report.source or cand.source
        if report.passed:
            self.state.bump("passed")
            issued = now()
            sig = self.signer.sign(FilterPass.payload(report, issued))
            if issued - self._last_pass_log.get(report.mint, 0) > 600:
                self._last_pass_log[report.mint] = issued
                store.add_decision(issued, "filter", "pass", report.mint, report.symbol, report.reason_text(),
                                   {"checks": [c.describe() for c in report.checks], "source": cand.source})
                self.emit("pass", f"PASSED all {len(report.checks)} filters -> Analyst", report.mint,
                          report.symbol, checks=[c.describe() for c in report.checks])
            self._last_reason.pop(report.mint, None)
            await self.desk.passes.put(FilterPass(report, issued, sig))
            return

        self.state.bump("rejected")
        reason = report.reason_text()
        key = ",".join(sorted(c.name for c in report.failed))
        if report.permanent_fail:
            self.desk.blacklist[report.mint] = reason
            self.state.bump("blacklisted")
        if self._last_reason.get(report.mint) == key and not report.permanent_fail:
            return  # same failure as last time: counted, not re-logged
        self._last_reason[report.mint] = key
        if len(self._last_reason) > 20000:
            self._last_reason.clear()
        store.add_decision(time.time(), "filter", "kill" if report.permanent_fail else "reject",
                           report.mint, report.symbol, reason,
                           {"checks": [c.describe() for c in report.checks], "source": cand.source,
                            "attempt": cand.attempts})
        # Only surface rejections that got past the cheap market checks, or permanent kills,
        # in the live activity feed (everything is still written to the database).
        deep = any(c.name not in ("market", "volume_1h", "liquidity", "venue", "graduation_wait") for c in report.checks)
        self.emit("reject" if not report.permanent_fail else "kill",
                  f"{'KILLED' if report.permanent_fail else 'rejected'}: {reason}", report.mint, report.symbol,
                  deep=deep)
