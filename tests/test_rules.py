"""Offline tests for the desk's non-negotiable rules."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from desk.agents.analyst import score_token
from desk.agents.executor import Executor, position_size
from desk.agents.filter import lp_lock_breakdown, lp_locked_pct, top10_pct
from desk.agents.risk import Risk, exit_decision
from desk.brokers.paper import PaperBroker
from desk.config import Config, RiskCfg, enforce_hard_limits
from desk.context import Desk
from desk.hub import Hub
from desk.models import (Check, ClearanceAuthority, FilterPass, FilterReport, MarketSnapshot, Position,
                         Verdict, now)
from desk.portfolio import AveragingDownError, Portfolio
from desk.store import Store


# ── helpers ──
def make_desk(tmp_path: Path) -> Desk:
    cfg = Config()
    cfg.data_dir = tmp_path
    store = Store(tmp_path / "paper.db")
    pf = Portfolio(store, "paper", 10.0)
    return Desk(cfg=cfg, hub=Hub(), store=store, http=None, dex=None, rug=None, jup=None, rpc=None,
                portfolio=pf, authority=ClearanceAuthority())


def snap(**kw) -> MarketSnapshot:
    base = dict(mint="M", symbol="TST", name="Test", pair_address="P", dex_id="pumpswap", price_usd=0.001,
                price_native=0.00001, liquidity_usd=80_000, volume_h1=120_000, volume_m5=9_000, buys_h1=900,
                sells_h1=600, buys_m5=80, sells_m5=55, change_m5=2.0, change_h1=35.0, change_h6=60.0,
                fdv=500_000, market_cap=500_000, pair_created_at=time.time() - 3 * 3600,
                websites=[], socials={"twitter": "https://x.com/t", "telegram": "https://t.me/t"})
    base.update(kw)
    return MarketSnapshot(**base)


def position(cost=1.0, value=1.0, peak=None) -> Position:
    return Position(mint="M", symbol="TST", tokens=1000, decimals=6, cost_sol=cost, entry_price_sol=cost / 1000,
                    entry_ts=time.time(), entry_liquidity_usd=80_000, exit_cost_pct=1.0, score=0.7,
                    entry_reason="test", mark_value_sol=value, peak_value_sol=peak if peak is not None else value)


def signed_verdict(desk: Desk, mint="M", score=0.72, all_pass=True, age=0.0, analysis=0.0) -> Verdict:
    """age: how old the Analyst verdict is; analysis: seconds the Analyst spent after the Filter signed."""
    checks = [Check("volume_1h", True), Check("mint_revoked", True), Check("top10_holders", all_pass)]
    rep = FilterReport(mint, "TST", all_pass, checks, market=snap(mint=mint))
    t = now() - age
    tf = t - analysis
    fsig = desk.authority.signer("filter").sign(FilterPass.payload(rep, tf))
    fp = FilterPass(rep, tf, fsig)
    trade = score >= 0.6
    asig = desk.authority.signer("analyst").sign(Verdict.payload(mint, score, trade, fsig, t))
    return Verdict(mint, "TST", score, {}, [], {}, fp, t, asig, trade)


# ── config hard limits ──
def test_config_cannot_loosen_mandate():
    cfg = Config()
    cfg.executor.max_position_pct = 25
    cfg.analyst.min_score = 0.3
    cfg.risk.hard_stop_pct = -15
    cfg.risk.trail_activate_pct = 5
    cfg.filter.max_top10_pct = 40
    cfg.filter.min_volume_h1_usd = 10_000
    cfg.filter.require_mint_revoked = False
    enforce_hard_limits(cfg)
    assert cfg.executor.max_position_pct == 8.0
    assert cfg.analyst.min_score == 0.6
    assert cfg.risk.hard_stop_pct == -6.0
    assert cfg.risk.trail_activate_pct == 12.0
    assert cfg.filter.max_top10_pct == 20.0
    assert cfg.filter.min_volume_h1_usd == 50_000
    assert cfg.filter.require_mint_revoked is True
    assert len(cfg.warnings) == 7


def test_config_can_be_stricter():
    cfg = Config()
    cfg.executor.max_position_pct = 4
    cfg.risk.hard_stop_pct = -4
    cfg.analyst.min_score = 0.7
    enforce_hard_limits(cfg)
    assert (cfg.executor.max_position_pct, cfg.risk.hard_stop_pct, cfg.analyst.min_score) == (4, -4, 0.7)


# ── sizing ──
@pytest.mark.parametrize("cash", [0.0, 0.3, 1, 10, 123.456789, 1e6])
@pytest.mark.parametrize("pct", [1, 8, 20, 100])
def test_position_size_never_exceeds_8pct(cash, pct):
    assert position_size(cash, pct) <= cash * 0.08 + 1e-12


# ── risk exits ──
def test_hard_stop_triggers_at_minus_6():
    r = RiskCfg()
    assert exit_decision(position(1.0, 0.95), r, time.time(), 80_000)[0] is None     # -5%
    assert exit_decision(position(1.0, 0.94), r, time.time(), 80_000)[0] == "HARD_STOP"  # -6%


def test_trailing_only_after_plus_12():
    r = RiskCfg()
    p = position(1.0, 1.11, peak=1.11)          # +11%: not armed
    assert exit_decision(p, r, time.time(), 80_000)[0] is None and not p.trailing_active
    p.mark_value_sol = 1.02                      # dropped from +11% to +2%: no trail exit (not armed)
    assert exit_decision(p, r, time.time(), 80_000)[0] is None
    p = position(1.0, 1.20, peak=1.20)           # +20%: armed
    assert exit_decision(p, r, time.time(), 80_000)[0] is None and p.trailing_active
    p.mark_value_sol = 1.20 * 0.95               # -5% from peak: hold
    assert exit_decision(p, r, time.time(), 80_000)[0] is None
    p.mark_value_sol = 1.20 * 0.94               # -6% from peak: exit
    assert exit_decision(p, r, time.time(), 80_000)[0] == "TRAILING_STOP"


def test_liquidity_pull_exit():
    assert exit_decision(position(1.0, 1.0), RiskCfg(), time.time(), 40_000)[0] == "LIQUIDITY_PULL"


# ── entry permissions ──
def test_no_averaging_down_and_no_revenge(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)
    desk.portfolio.open(position())
    ok, why = risk.approve_entry("M")
    assert not ok and "averaging down" in why
    with pytest.raises(AveragingDownError):
        desk.portfolio.open(position())
    trade = desk.portfolio.close("M", 0.9, "HARD_STOP")
    risk.record_result(trade)
    ok, why = risk.approve_entry("M")
    assert not ok and "revenge" in why
    ok, why = risk.approve_entry("OTHER")
    assert not ok and "cooldown" in why
    risk.last_loss_ts = 0
    assert risk.approve_entry("OTHER")[0]


def test_losing_streak_pause(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)
    for i in range(3):
        risk.record_result({"mint": f"m{i}", "pnl_sol": -0.05})
    risk.last_loss_ts = 0
    ok, why = risk.approve_entry("new")
    assert not ok and "streak" in why


# ── executor clearance (hard handoff) ──
def test_executor_refuses_forged_or_incomplete_clearance(tmp_path):
    desk = make_desk(tmp_path)
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    assert ex.verify_clearance(signed_verdict(desk))[0]
    # failed filter check
    assert not ex.verify_clearance(signed_verdict(desk, all_pass=False))[0]
    # below threshold
    assert not ex.verify_clearance(signed_verdict(desk, score=0.59))[0]
    # expired
    assert not ex.verify_clearance(signed_verdict(desk, age=600))[0]
    # a slow website check (Analyst busy 100 s after the Filter signed) must not expire a fresh verdict...
    assert ex.verify_clearance(signed_verdict(desk, age=5, analysis=100))[0]
    # ...but a Filter clearance older than ttl + the Analyst's maximum working time is refused
    assert not ex.verify_clearance(signed_verdict(desk, age=5, analysis=400))[0]
    # tampered score after signing
    v = signed_verdict(desk, score=0.61)
    v.score = 0.99
    assert not ex.verify_clearance(v)[0]
    # signed by a different authority (not this desk)
    (tmp_path / "x").mkdir()
    other = make_desk(tmp_path / "x")
    assert not ex.verify_clearance(signed_verdict(other))[0]


def test_executor_size_is_8pct_of_current_balance(tmp_path):
    desk = make_desk(tmp_path)
    desk.portfolio.set_cash(7.5)
    seen = {}

    class FakeJup:
        async def quote(self, i, o, amount, bps):
            if i.startswith("So111"):
                if amount != 10_000_000:          # 0.01 SOL = the Executor's reference quote
                    seen["lamports"] = amount
                return {"inAmount": str(amount), "outAmount": str(int(amount / 1e9 / 0.00001 * 1e6))}
            return {"outAmount": str(int(seen["lamports"] * 0.98))}

    class FakeDex:
        async def snapshot(self, mint, fresh=True):
            return snap(mint=mint)

    desk.jup, desk.dex = FakeJup(), FakeDex()
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6
    asyncio.run(ex.handle_verdict(v))
    assert seen["lamports"] / 1e9 == pytest.approx(7.5 * 0.08, rel=1e-6)
    assert "M" in desk.portfolio.positions
    assert desk.portfolio.positions["M"].cost_sol <= 7.5 * 0.08 + 0.001


# ── filter helpers ──
def test_lp_lock_bonding_curve_and_weighting():
    rc = {"markets": [
        {"marketType": "pump_fun", "lp": {"quoteUSD": 10_000, "baseUSD": 10_000, "lpLockedPct": 0}},
    ]}
    assert lp_locked_pct(rc)[0] == 100
    rc = {"markets": [
        {"marketType": "pump_fun_amm", "lp": {"quoteUSD": 45_000, "baseUSD": 45_000, "lpLockedPct": 100}},
        {"marketType": "meteoradlmm", "lp": {"quoteUSD": 5_000, "baseUSD": 5_000, "lpLockedPct": 0}},
    ]}
    assert lp_locked_pct(rc)[0] == pytest.approx(90.0)          # strict metric: DLMM counts as unlocked
    b = lp_lock_breakdown(rc)
    assert b["locked_pct"] == pytest.approx(100.0)                # DLMM has no LP token: not in the lock %
    assert b["unlockable_pct"] == pytest.approx(10.0)             # ...but its share is measured
    assert lp_locked_pct({"markets": []})[0] is None
    assert lp_lock_breakdown({"markets": []})["locked_pct"] is None
    # an unburned launch pool still fails, whatever concentrated pools sit next to it
    rc = {"markets": [
        {"marketType": "pump_fun_amm", "lp": {"quoteUSD": 45_000, "baseUSD": 45_000, "lpLockedPct": 0}},
        {"marketType": "meteoradlmm", "lp": {"quoteUSD": 5_000, "baseUSD": 5_000, "lpLockedPct": 0}},
    ]}
    assert lp_lock_breakdown(rc)["locked_pct"] == 0
    # only concentrated pools: nothing lockable at all
    rc = {"markets": [{"marketType": "meteoradlmm", "lp": {"quoteUSD": 5_000, "baseUSD": 5_000, "lpLockedPct": 0}}]}
    assert lp_lock_breakdown(rc)["locked_pct"] is None


def test_filter_lp_check_caps_unlockable_liquidity(tmp_path):
    from desk.agents.filter import Filter
    from desk.models import Candidate

    def report(dlmm_usd):
        return {"token": {"supply": 1e9, "decimals": 6}, "creator": "DEV",
                "markets": [{"marketType": "pump_fun_amm", "pubkey": "POOL",
                             "lp": {"quoteUSD": 50_000, "baseUSD": 50_000, "lpLockedPct": 100}},
                            {"marketType": "meteoradlmm", "pubkey": "DLMM",
                             "lp": {"quoteUSD": dlmm_usd / 2, "baseUSD": dlmm_usd / 2, "lpLockedPct": 0}}],
                "topHolders": [{"owner": "POOL", "pct": 20.0}] + [{"owner": f"W{i}", "pct": 1.0} for i in range(10)]}

    class Rug:
        rc = None

        async def report(self, mint):
            return self.rc

    class Rpc:
        async def mint_info(self, mint):
            return {"mintAuthority": None, "freezeAuthority": None, "decimals": 6}

    desk = make_desk(tmp_path)
    desk.rug, desk.rpc = Rug(), Rpc()
    f = Filter(desk)

    def lp_check(dlmm_usd, max_unl=50.0):
        desk.rug.rc = report(dlmm_usd)
        f.f.max_unlockable_liquidity_pct = max_unl
        rep = asyncio.run(f.deep(Candidate("M"), snap()))
        return next(c for c in rep.checks if c.name == "lp_locked")

    assert lp_check(30_000).passed                    # 23% in DLMM: launch LP burned -> pass (used to fail at 77%)
    assert not lp_check(150_000).passed               # 60% of liquidity pullable -> fail
    assert not lp_check(30_000, max_unl=0).passed     # 0 = old strict rule


def test_top10_excludes_pools():
    rc = {
        "markets": [{"pubkey": "POOL", "liquidityAAccount": {"owner": "POOL"}}],
        "knownAccounts": {"POOL": {"type": "AMM"}},
        "topHolders": [{"owner": "POOL", "address": "a", "pct": 30}]
                      + [{"owner": f"w{i}", "address": f"b{i}", "pct": 1.5} for i in range(12)],
    }
    pct, n = top10_pct(rc)
    assert pct == pytest.approx(15.0) and n == 12


# ── analyst ──
def test_score_bounds_and_caps():
    web = {"url": "https://example.org", "score": 0.8, "legit": 0.8, "tasty": 0.7, "flags": []}
    rug = {"top10_pct": 9, "holders_total": 2500, "insider_pct": 3, "risks": []}
    good, _, _ = score_token(snap(), rug, web)
    assert 0.6 <= good <= 1.0
    parabolic, _, why = score_token(snap(change_h1=600), rug, web)
    assert parabolic <= 0.5 and any("CAP" in w for w in why)
    insiders, _, _ = score_token(snap(), {**rug, "insider_pct": 55}, web)
    assert insiders <= 0.4
    wash, _, _ = score_token(snap(volume_h1=80_000 * 20), rug, web)
    assert wash <= 0.45


def test_no_falling_knife_or_late_entry():
    web = {"url": "https://example.org", "score": 0.8, "legit": 0.8, "tasty": 0.7, "flags": []}
    rug = {"top10_pct": 9, "holders_total": 2500, "insider_pct": 3, "risks": []}
    knife, _, why = score_token(snap(change_m5=-5.4), rug, web)
    assert knife < 0.6 and any("falling knife" in w for w in why)
    late, _, why = score_token(snap(change_h6=695), rug, web)
    assert late < 0.6 and any("late entry" in w for w in why)


def test_missing_website_is_ignored_not_penalised():
    rug = {"top10_pct": 9, "holders_total": 2500, "insider_pct": 3, "risks": []}
    no_site = {"url": None, "score": 0.3, "legit": 0.35, "tasty": 0.2, "flags": ["no website"]}
    dead_site = {"url": "https://dead.example", "score": 0.08, "legit": 0.1, "tasty": 0.0,
                 "flags": ["website HTTP 404"]}
    good_site = {"url": "https://good.example", "score": 0.9, "legit": 0.9, "tasty": 0.8, "flags": []}
    none_s, b, why = score_token(snap(), rug, no_site)
    dead_s, _, _ = score_token(snap(), rug, dead_site)
    good_s, _, _ = score_token(snap(), rug, good_site)
    assert any("no website" in w for w in why)
    assert b["website"] == pytest.approx(0.8)  # only socials scored (twitter + telegram in snap())
    # a missing site is neutral: better than a broken one, and only a strong real site beats it
    assert dead_s < none_s
    assert 0.0 <= none_s <= 1.0 and good_s > dead_s
    # the result equals the score with the website share of the weight removed entirely
    from desk.agents.analyst import WEIGHTS
    w = dict(WEIGHTS); w["website"] *= 0.3
    _, b2, _ = score_token(snap(), rug, no_site)
    assert none_s == pytest.approx(sum(w[k] * b2[k] for k in w) / sum(w.values()), abs=1e-3)


def test_scout_rug_flag_needs_confirmation(tmp_path):
    """One bad DexScreener reading must not blacklist a token and put its dev in the rug ledger forever."""
    from desk.agents.scout import Scout, Watch
    from desk.models import Candidate

    desk = make_desk(tmp_path)

    class Dex:
        liq = 1_500_000.0

        async def snapshot(self, mint, **kw):
            return snap(mint=mint, liquidity_usd=self.liq)

    desk.dex = Dex()
    sc = Scout(desk)
    desk.creators["M"] = "DEV"
    w = Watch(Candidate("M"), added=time.time(), max_liq=1_600_000)
    desk.market_seen["M"] = (time.time(), 90_000, 1_936)          # glitchy batched reading
    asyncio.run(sc._check_rug("M", w))
    assert "M" not in desk.blacklist and desk.store.rugger("DEV") is None
    desk.dex.liq = 1_000.0                                         # confirmed collapse
    desk.market_seen["M"] = (time.time(), 90_000, 1_000)
    asyncio.run(sc._check_rug("M", w))
    assert "M" in desk.blacklist and desk.store.rugger("DEV") is not None


def test_entry_impact_uses_live_reference_quote(tmp_path):
    """DexScreener lags: impact is measured against a tiny Jupiter quote, not the DexScreener price."""
    desk = make_desk(tmp_path)
    rejected = []

    class FakeJup:
        async def quote(self, i, o, amount, bps, lane=""):
            if i.startswith("So111"):
                # live price is 10% above DexScreener's (0.00001 SOL), 1% impact for the real size
                per_sol = 1 / 0.000011 * 1e6 * (0.99 if amount > 10_000_000 else 1.0)
                return {"inAmount": str(amount), "outAmount": str(int(amount / 1e9 * per_sol))}
            return {"outAmount": str(int(amount * 0.000011 / 1e6 * 1e9 * 0.98))}

    class FakeDex:
        async def snapshot(self, mint, fresh=True):
            return snap(mint=mint)

    desk.jup, desk.dex = FakeJup(), FakeDex()
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    ex.reject = lambda v, why: rejected.append(why)
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6
    asyncio.run(ex.handle_verdict(v))
    assert not any("impact" in r for r in rejected), rejected
    assert "M" in desk.portfolio.positions
