"""ANALYST — scores every Filter survivor from 0 to 1. Under 0.6 = no trade.

Score = weighted blend of eight components (weights sum to 1):
  liquidity depth .14 | turnover (vol/liq) .10 | buy pressure .16 | momentum .12
  holder quality .14 | contract hygiene .08 | website & socials .16 | age/maturity .10
Then hard caps for wash trading, insider clusters and parabolic blow-off tops.
Every verdict carries its full breakdown and human-readable rationale.
"""
from __future__ import annotations

import asyncio
import math

from ..models import FilterPass, MarketSnapshot, Verdict, now
from ..website import WebsiteInspector, pick_website
from .base import Agent

WEIGHTS = {
    "liquidity": 0.14, "turnover": 0.10, "buy_pressure": 0.16, "momentum": 0.12,
    "holders": 0.14, "hygiene": 0.08, "website": 0.16, "maturity": 0.10,
}


def _lerp(x: float, x0: float, x1: float) -> float:
    if x1 == x0:
        return 0.0
    return max(0.0, min(1.0, (x - x0) / (x1 - x0)))


def _band(x: float, lo0: float, lo1: float, hi1: float, hi0: float) -> float:
    """1 inside [lo1, hi1], ramps to 0 at lo0 and hi0."""
    if x <= lo0 or x >= hi0:
        return 0.0
    if x < lo1:
        return _lerp(x, lo0, lo1)
    if x > hi1:
        return 1 - _lerp(x, hi1, hi0)
    return 1.0


def score_token(m: MarketSnapshot, rug: dict, web: dict) -> tuple[float, dict, list[str]]:
    b: dict[str, float] = {}
    why: list[str] = []

    b["liquidity"] = _lerp(math.log10(max(m.liquidity_usd, 1)), math.log10(20_000), math.log10(400_000))
    why.append(f"liquidity ${m.liquidity_usd:,.0f}")

    turnover = m.volume_h1 / max(m.liquidity_usd, 1)
    b["turnover"] = _band(turnover, 0.2, 0.8, 4.0, 12.0)
    why.append(f"1h turnover {turnover:.1f}x liquidity")

    tot_h1 = m.buys_h1 + m.sells_h1
    tot_m5 = m.buys_m5 + m.sells_m5
    br_h1 = m.buys_h1 / tot_h1 if tot_h1 else 0.5
    br_m5 = m.buys_m5 / tot_m5 if tot_m5 else 0.5
    b["buy_pressure"] = 0.5 * _band(br_h1, 0.42, 0.53, 0.72, 0.9) + 0.5 * _band(br_m5, 0.38, 0.5, 0.75, 0.92)
    why.append(f"buys {br_h1:.0%} (1h) / {br_m5:.0%} (5m), {tot_h1} txns/h")

    mom = _band(m.change_h1, -15, 5, 80, 300)
    if m.change_m5 < -8:
        mom *= 0.4
        why.append(f"dumping now ({m.change_m5:+.1f}% 5m)")
    b["momentum"] = mom
    why.append(f"price {m.change_m5:+.1f}% 5m / {m.change_h1:+.1f}% 1h / {m.change_h6:+.1f}% 6h")

    t10 = rug.get("top10_pct") or 20.0
    holders = float(rug.get("holders_total") or 0)
    ins = float(rug.get("insider_pct") or 0)
    h = 0.45 * (1 - _lerp(t10, 6, 20)) + 0.35 * _lerp(math.log10(max(holders, 1)), 2.0, 3.6) \
        + 0.20 * (1 - _lerp(ins, 5, 35))
    b["holders"] = max(0.0, min(1.0, h))
    why.append(f"top10 {t10:.1f}%, {int(holders)} holders, insider clusters {ins:.0f}%")

    risks = rug.get("risks") or []
    warn = sum(1 for _, lvl in risks if lvl == "warn")
    hyg = 1.0 - 0.15 * warn - (0.15 if rug.get("mutable_metadata") else 0)
    b["hygiene"] = max(0.0, hyg)
    if warn:
        why.append(f"{warn} RugCheck warnings")

    soc = m.socials or {}
    social_pts = (0.5 if "twitter" in soc else 0) + (0.3 if "telegram" in soc else 0) + \
                 (0.2 if len(soc) >= 3 else 0)
    b["website"] = 0.7 * float(web.get("score", 0.3)) + 0.3 * social_pts
    why.append(f"website {web.get('score', 0):.2f} (legit {web.get('legit', 0):.2f} / tasty "
               f"{web.get('tasty', 0):.2f}), socials: {', '.join(soc) or 'none'}")

    age = m.age_minutes
    b["maturity"] = _band(age, 4, 25, 12 * 60, 7 * 24 * 60) * 0.85 + 0.15
    why.append(f"age {age:.0f} min")

    score = sum(WEIGHTS[k] * b[k] for k in WEIGHTS)

    # Hard caps — any one of these makes the token untradeable regardless of the rest.
    caps = []
    if turnover > 15:
        caps.append(("wash-trading pattern (turnover > 15x)", 0.45))
    if ins > 40:
        caps.append((f"insider clusters hold {ins:.0f}%", 0.4))
    if m.change_h1 > 400 or m.change_m5 > 60:
        caps.append(("parabolic blow-off, chasing risk", 0.5))
    if web.get("flags") and any("scam" in f or "drainer" in f for f in web["flags"]):
        caps.append(("scam language on website", 0.3))
    if br_m5 < 0.3 and tot_m5 >= 20:
        caps.append(("heavy active selling", 0.45))
    for reason, cap in caps:
        if score > cap:
            score = cap
            why.append(f"CAP {cap:.2f}: {reason}")
    return round(max(0.0, min(1.0, score)), 4), {k: round(v, 3) for k, v in b.items()}, why


class Analyst(Agent):
    name = "Analyst"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        a = self.cfg.analyst
        self.signer = desk.authority.signer("analyst")
        use_claude = a.claude_website_review and self.cfg.secrets.anthropic_api_key_present
        self.web = WebsiteInspector(use_claude, a.claude_model)
        self.claude_on = use_claude
        self._last_scored: dict[str, float] = {}
        self._last_logged: dict[str, tuple[float, float]] = {}

    async def run(self) -> None:
        self.status("idle")
        self.emit("info", f"Analyst online: threshold {self.cfg.analyst.min_score}, website checks "
                          f"{'ON' if self.cfg.analyst.website_check else 'off'}, Claude review "
                          f"{'ON' if self.claude_on else 'off (no ANTHROPIC_API_KEY)'}")
        await self.supervised("analyst", self.loop)

    async def loop(self) -> None:
        while not self.desk.stop.is_set():
            try:
                fp = await asyncio.wait_for(self.desk.passes.get(), timeout=2)
            except asyncio.TimeoutError:
                self.status("idle")
                continue
            # Re-score the same token at most every 90s (the Scout keeps re-submitting survivors).
            last = self._last_scored.get(fp.report.mint, 0)
            if now() - last < 90:
                continue
            self._last_scored[fp.report.mint] = now()
            self.status("working")
            await self.analyze(fp)

    async def analyze(self, fp: FilterPass) -> None:
        rep = fp.report
        m = rep.market
        # Hard handoff: refuse anything without a valid Filter clearance.
        if not self.desk.authority.verify("filter", FilterPass.payload(rep, fp.issued_at), fp.signature) \
                or not rep.passed:
            self.emit("error", "REFUSED: candidate without valid Filter clearance", rep.mint, rep.symbol)
            return
        if m is None:
            return
        # Use the freshest market data, not what the Filter saw seconds ago.
        fresh = await self.desk.dex.snapshot(rep.mint)
        if fresh:
            m = fresh
        web = {"score": 0.3, "legit": 0.35, "tasty": 0.2, "flags": ["website check disabled"], "notes": []}
        if self.cfg.analyst.website_check:
            extra = []
            meta = {}
            if not pick_website(m.websites):
                meta = await self.web.metadata_links(rep.rug.get("metadata_uri"))
                if meta.get("website"):
                    extra.append(meta["website"])
            socials = dict(m.socials)
            for k in ("twitter", "telegram"):
                if meta.get(k) and k not in socials:
                    socials[k] = meta[k]
            try:
                web = await asyncio.wait_for(
                    self.web.inspect(pick_website(m.websites, extra), mint=m.mint, symbol=m.symbol,
                                     name=m.name, socials=socials), timeout=90)
            except asyncio.TimeoutError:
                web = {"score": 0.2, "legit": 0.2, "tasty": 0.1, "flags": ["website check timed out"], "notes": []}

        score, breakdown, why = score_token(m, rep.rug, web)
        min_score = self.cfg.analyst.min_score
        trade = score >= min_score
        issued = now()
        sig = self.signer.sign(Verdict.payload(rep.mint, score, trade, fp.signature, issued))
        verdict = Verdict(rep.mint, m.symbol, score, breakdown, why, web, fp, issued, sig, trade)
        self.state.bump("scored")
        if self.desk.shadow is not None:
            self.desk.shadow.start(rep.mint, m.symbol, "analyst", "approve" if trade else "reject_score",
                                   "; ".join(why[:3]), m.price_usd, m.liquidity_usd, score=score)
        prev = self._last_logged.get(rep.mint)
        quiet = (not trade and prev is not None and abs(prev[0] - score) < 0.03 and issued - prev[1] < 600)
        if quiet:
            return
        self._last_logged[rep.mint] = (score, issued)
        summary = f"score {score:.2f} {'>= ' if trade else '< '}{min_score} -> {'BUY SIGNAL' if trade else 'no trade'}"
        self.desk.store.add_decision(issued, "analyst", "approve" if trade else "reject", rep.mint, m.symbol,
                                     summary + " | " + "; ".join(why),
                                     {"breakdown": breakdown, "website": {k: web.get(k) for k in
                                      ("url", "legit", "tasty", "flags", "notes", "claude")}})
        if trade:
            self.state.bump("approved")
            self.emit("score", summary, rep.mint, m.symbol, breakdown=breakdown, why=why)
            await self.desk.verdicts.put(verdict)
        else:
            self.state.bump("rejected")
            weakest = sorted(breakdown.items(), key=lambda kv: kv[1])[:2]
            self.emit("reject", f"{summary} (weakest: {', '.join(f'{k} {v:.2f}' for k, v in weakest)})",
                      rep.mint, m.symbol, breakdown=breakdown, why=why, deep=True)
