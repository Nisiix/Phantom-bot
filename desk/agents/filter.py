"""FILTER — kills anything that fails a single safety rule.

Mandatory kills (user rules):
  1. mint authority not revoked                   (verified on-chain + RugCheck)
  2. LP not locked/burned (>= min_lp_locked_pct)  (liquidity-weighted over all pools)
  3. top-10 holders > 20%                          (pools / bonding curve excluded)
  4. last-hour volume < $50k                       (all pairs)
  5. dev wallet connected to previous rugs         (RugCheck creator history, our own
                                                    rug ledger, serial dead-launch pattern)
Extra capital-preservation kills (config): freeze authority, dangerous Token-2022
extensions, danger-level RugCheck risks, minimum liquidity.

Only a report where EVERY check passed is signed and handed to the Analyst.
Every rejection is logged with its reasons. A token that keeps failing for the
same reasons is logged again only when its reasons change (no log spam).
"""
from __future__ import annotations

import asyncio
import time

from ..models import Candidate, Check, FilterPass, FilterReport, MarketSnapshot, now
from .base import Agent

OVERLAP_RISK_WORDS = ("lp", "liquidity", "holder", "mint authority", "freeze authority", "rug",
                      "ownership", "insider")
BONDING_CURVE_MARKETS = {"pump_fun", "pumpfun", "raydium_launchlab", "launchlab", "meteora_dbc", "letsbonk"}


def lp_locked_pct(report: dict) -> tuple[float | None, str]:
    """Liquidity-weighted LP locked %. Bonding curves are program-held => locked."""
    total, locked, kinds = 0.0, 0.0, []
    for m in report.get("markets") or []:
        mt = (m.get("marketType") or "").lower()
        lp = m.get("lp") or {}
        usd = float(lp.get("quoteUSD") or 0) + float(lp.get("baseUSD") or 0)
        if usd <= 0:
            continue
        pct = 100.0 if mt in BONDING_CURVE_MARKETS else float(lp.get("lpLockedPct") or 0)
        total += usd
        locked += usd * pct / 100
        kinds.append(f"{mt}:{pct:.0f}%")
    if total <= 0:
        return None, "no LP market found"
    return locked / total * 100, ", ".join(kinds[:4])


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


class Filter(Agent):
    name = "Filter"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.signer = desk.authority.signer("filter")
        self.f = self.cfg.filter
        self._last_reason: dict[str, str] = {}
        self._last_pass_log: dict[str, float] = {}

    async def run(self) -> None:
        self.status("idle")
        self.emit("info", "Filter online: mint/LP/top10/volume/dev-rug kills armed")
        await self.supervised("filter", self.loop)

    async def loop(self) -> None:
        q = self.desk.candidates
        while not self.desk.stop.is_set():
            try:
                first = await asyncio.wait_for(q.get(), timeout=2)
            except asyncio.TimeoutError:
                self.status("idle")
                continue
            batch = [first]
            while len(batch) < 30 and not q.empty():
                batch.append(q.get_nowait())
            batch = list({c.mint: c for c in batch if c.mint not in self.desk.blacklist}.values())
            if not batch:
                continue
            self.status("working")
            snaps = await self.desk.dex.snapshots([c.mint for c in batch])
            for cand in batch:
                try:
                    report = await self.evaluate(cand, snaps.get(cand.mint))
                except Exception as e:  # noqa: BLE001
                    self.emit("error", f"evaluation failed: {e}", cand.mint, cand.symbol)
                    continue
                await self.route(cand, report)

    # ── core evaluation ──
    async def evaluate(self, cand: Candidate, snap: MarketSnapshot | None) -> FilterReport:
        self.state.bump("evaluated")
        f = self.f
        sym = snap.symbol if snap else cand.symbol
        if snap is None:
            return FilterReport(cand.mint, sym, False, [Check("market", False, "none", "listed",
                                                               "no DEX pair indexed yet")])
        self.desk.market_seen[cand.mint] = (time.time(), snap.volume_h1, snap.liquidity_usd)
        checks = [
            Check("volume_1h", snap.volume_h1 >= f.min_volume_h1_usd, round(snap.volume_h1),
                  f.min_volume_h1_usd, f"${snap.volume_h1:,.0f}/h < ${f.min_volume_h1_usd:,.0f}"
                  if snap.volume_h1 < f.min_volume_h1_usd else f"${snap.volume_h1:,.0f}/h"),
            Check("liquidity", snap.liquidity_usd >= f.min_liquidity_usd, round(snap.liquidity_usd),
                  f.min_liquidity_usd, f"${snap.liquidity_usd:,.0f}"),
        ]
        if not all(c.passed for c in checks):
            # Cheap market checks failed: stop here, don't burn RugCheck quota.
            return FilterReport(cand.mint, sym, False, checks, market=snap)

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

        # 2. LP locked
        lp_pct, lp_detail = lp_locked_pct(rc)
        checks.append(Check("lp_locked", lp_pct is not None and lp_pct >= f.min_lp_locked_pct,
                            None if lp_pct is None else round(lp_pct, 1), f.min_lp_locked_pct,
                            f"{lp_pct:.1f}% locked [{lp_detail}]" if lp_pct is not None else lp_detail))

        # 3. Top 10 holders
        t10, n_holders = top10_pct(rc)
        checks.append(Check("top10_holders", t10 is not None and t10 <= f.max_top10_pct,
                            None if t10 is None else round(t10, 2), f.max_top10_pct,
                            f"top10 hold {t10:.1f}% (pools excluded)" if t10 is not None else "holder data missing"))

        # 5. Dev wallet connected to previous rugs
        rug_reasons = []
        if rc.get("rugged"):
            rug_reasons.append("RugCheck marks this token rugged")
        for r in rc.get("risks") or []:
            nm = (r.get("name") or "").lower()
            if "rug" in nm:
                rug_reasons.append(r.get("name"))
        ledger = self.desk.store.ruggers()
        if creator and creator in ledger:
            rug_reasons.append(f"dev in rug ledger ({ledger[creator]['reason'][:60]})")
        prior = [t for t in (rc.get("creatorTokens") or []) if t.get("mint") != cand.mint]
        dead = [t for t in prior if float(t.get("marketCap") or 0) < 10_000]
        if len(prior) >= f.serial_dev_min_launches and len(dead) / len(prior) >= f.serial_dev_dead_ratio:
            rug_reasons.append(f"serial dev: {len(dead)}/{len(prior)} prior launches dead")
        checks.append(Check("dev_rug_link", not rug_reasons, "; ".join(rug_reasons) or "clean", "clean",
                            "; ".join(rug_reasons) or f"dev {str(creator)[:6]} clean ({len(prior)} prior)",
                            permanent=bool(rug_reasons)))
        if rug_reasons and creator:
            self.desk.store.add_rugger(creator, cand.mint, rug_reasons[0][:120])

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

        passed = all(c.passed for c in checks)
        rug_facts = {
            "top10_pct": t10, "holders_total": rc.get("totalHolders") or n_holders,
            "insider_pct": insider_pct(rc), "insider_networks": len(rc.get("insiderNetworks") or []),
            "lp_locked_pct": lp_pct, "risks": [(r.get("name"), r.get("level")) for r in rc.get("risks") or []],
            "creator": creator, "creator_prior_launches": len(prior),
            "mutable_metadata": bool((rc.get("tokenMeta") or {}).get("mutable")),
            "launchpad": (rc.get("launchpad") or {}).get("name"),
            "metadata_uri": (rc.get("tokenMeta") or {}).get("uri"),
            "decimals": token.get("decimals"),
        }
        return FilterReport(cand.mint, sym, passed, checks, market=snap, rug=rug_facts, creator=creator)

    async def route(self, cand: Candidate, report: FilterReport) -> None:
        store = self.desk.store
        if self.desk.shadow is not None and report.market is not None:
            outcome = "pass" if report.passed else ("kill" if report.permanent_fail else "reject")
            self.desk.shadow.start(report.mint, report.symbol, "filter", outcome, report.reason_text(),
                                   report.market.price_usd, report.market.liquidity_usd)
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
        deep = any(c.name not in ("market", "volume_1h", "liquidity") for c in report.checks)
        self.emit("reject" if not report.permanent_fail else "kill",
                  f"{'KILLED' if report.permanent_fail else 'rejected'}: {reason}", report.mint, report.symbol,
                  deep=deep)
