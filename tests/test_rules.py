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
    r = RiskCfg(profit_lock_activate_pct=0)      # trailing alone (the profit lock has its own tests)
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
    # The post-loss block is per token: other pairs stay open right away.
    assert risk.approve_entry("OTHER")[0]


def test_no_losing_streak_pause_by_default(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)
    for i in range(3):
        risk.record_result({"mint": f"m{i}", "pnl_sol": -0.05})
    assert risk.approve_entry("new")[0]


def test_losing_streak_pause(tmp_path):
    desk = make_desk(tmp_path)
    desk.cfg.risk.streak_pause_minutes = 30
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
    parabolic, _, why = score_token(snap(change_h1=600, pair_created_at=time.time() - 7 * 3600), rug, web)
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
    old = time.time() - 7 * 3600
    late, _, why = score_token(snap(change_h6=695, pair_created_at=old), rug, web)
    assert late < 0.6 and any("late entry" in w for w in why)
    # older than 4h: stricter caps (+350% in 6h, +250% in 1h are already too late)
    late, _, why = score_token(snap(change_h6=350, pair_created_at=old), rug, web)
    assert late < 0.6 and any("late entry" in w for w in why)
    late, _, why = score_token(snap(change_h1=250, change_h6=250, pair_created_at=old), rug, web)
    assert late < 0.6 and any("parabolic" in w for w in why)


def test_young_token_change_since_launch_is_not_a_late_entry():
    """Under 4h the 1h/6h change counts from launch: no late-entry / 1h parabolic cap, only buying the candle."""
    web = {"url": "https://example.org", "score": 0.8, "legit": 0.8, "tasty": 0.7, "flags": []}
    rug = {"top10_pct": 9, "holders_total": 2500, "insider_pct": 3, "risks": []}
    young = time.time() - 100 * 60
    s, _, why = score_token(snap(change_h6=624, change_h1=40, pair_created_at=young), rug, web)
    assert s >= 0.6 and not any("CAP" in w for w in why)
    s, _, why = score_token(snap(change_h1=1226, change_h6=1226, pair_created_at=young), rug, web)
    assert s >= 0.6 and not any("CAP" in w for w in why)
    s, _, why = score_token(snap(change_m5=75, change_h1=300, pair_created_at=young), rug, web)
    assert s < 0.6 and any("blow-off" in w for w in why)


def test_volume_floor_follows_token_age_and_liquidity(tmp_path):
    from desk.agents.filter import Filter
    f = Filter(make_desk(tmp_path))
    t = time.time()
    h = 3600
    # >= 4h (2026-11-14): half the liquidity, between $15k and $50k
    assert f.volume_floor(snap(pair_created_at=t - 5 * h, liquidity_usd=40_000)) == 20_000
    assert f.volume_floor(snap(pair_created_at=t - 5 * h, liquidity_usd=20_000)) == 15_000
    assert f.volume_floor(snap(pair_created_at=t - 5 * h, liquidity_usd=400_000)) == 50_000
    f.f.old_volume_liquidity_ratio = 0                                                       # 0 = fixed bar
    assert f.volume_floor(snap(pair_created_at=t - 5 * h, liquidity_usd=40_000)) == 50_000
    f.f.old_volume_liquidity_ratio = 0.5
    assert f.volume_floor(snap(pair_created_at=t - 2 * h, liquidity_usd=40_000)) == 20_000   # young: 0.5 x liq
    assert f.volume_floor(snap(pair_created_at=t - 2 * h, liquidity_usd=400_000)) == 50_000  # never above $50k
    assert f.volume_floor(snap(pair_created_at=t - 30 * 60, liquidity_usd=40_000)) == pytest.approx(10_000, rel=0.01)
    assert f.volume_floor(snap(pair_created_at=t - 3 * 60, liquidity_usd=20_000)) == 5_000   # minimum
    assert f.volume_floor(snap(pair_created_at=0)) == 50_000                                 # unknown age
    young = snap(pair_created_at=t - 2 * h, liquidity_usd=40_000, volume_h1=25_000)
    vol = next(c for c in f._market_checks(young) if c.name == "volume_1h")
    assert vol.passed and "young token" in vol.detail
    old = lambda vol: next(c for c in f._market_checks(snap(pair_created_at=t - 5 * h, volume_h1=vol))  # noqa: E731
                           if c.name == "volume_1h")
    assert not old(25_000).passed                      # $80k pool: floor $40k
    assert old(45_000).passed and "older token" in old(45_000).detail


def test_age_rules_cannot_be_loosened():
    cfg = Config()
    cfg.filter.young_volume_liquidity_ratio = 0.01
    cfg.filter.young_min_volume_usd = 100
    cfg.analyst.old_late_entry_h6_pct = 2000
    cfg.analyst.young_age_hours = 48
    enforce_hard_limits(cfg)
    assert cfg.filter.young_volume_liquidity_ratio == 0.5 and cfg.filter.young_min_volume_usd == 5_000
    assert cfg.analyst.old_late_entry_h6_pct == 300 and cfg.analyst.young_age_hours == 4


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
    if "traction" in b2:                      # snap() is a 3h-old token: young, so traction counts too
        w["traction"] = 0.14
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


# ── on-chain LP check (desk/onchain_lp.py) ──
class FakeChain:
    """Minimal JSON-RPC stand-in: LP mints, token accounts and concentrated-pool positions."""
    PDA = "3f7GcQFG397GAaEnv51zR6tsTVihYRydnydDD1cXekxH"           # off-curve (locker PDA)
    WALLET = "CF6qNim1x6UHyXStoDpqEtefD5P6ZyKKSYp7ygABRsZ9"        # on-curve (ordinary wallet)

    def __init__(self, lp_holders, dlmm_owners):
        self.lp_holders, self.dlmm_owners = lp_holders, dlmm_owners

    async def call(self, method, params=None):
        import base64 as b64
        from solders.pubkey import Pubkey
        if method == "getTokenSupply":
            return {"value": {"amount": str(sum(a for _, a in self.lp_holders))}}
        if method == "getTokenLargestAccounts":
            return {"value": [{"address": f"ACC{i}", "amount": str(a)} for i, (_, a) in enumerate(self.lp_holders)]}
        if method == "getMultipleAccounts":
            owners = {f"ACC{i}": o for i, (o, _) in enumerate(self.lp_holders)}
            return {"value": [{"data": {"parsed": {"info": {"owner": owners.get(a)}}}} for a in params[0]]}
        if method == "getProgramAccounts":
            return [{"account": {"data": [b64.b64encode(bytes(Pubkey.from_string(o))).decode(), "base64"]}}
                    for o in self.dlmm_owners]
        raise RuntimeError(method)


def test_onchain_lp_classic_and_dlmm_owners():
    from desk.onchain_lp import OnchainLP
    rc = {"markets": [
        {"pubkey": "AMM", "marketType": "raydium_cpmm", "mintLP": "LPMINT",
         "lp": {"quoteUSD": 40_000, "baseUSD": 40_000, "lpLockedPct": 100}},
        {"pubkey": "DLMM", "marketType": "meteoraDlmm", "mintLP": "11111111111111111111111111111111",
         "lp": {"quoteUSD": 10_000, "baseUSD": 10_000, "lpLockedPct": 0}},
    ]}
    W, P = FakeChain.WALLET, FakeChain.PDA
    # LP 100% in a locker PDA, DLMM provided by a stranger -> 100% locked, DLMM verified third-party
    res = asyncio.run(OnchainLP(FakeChain([(P, 1000)], [W]), False).analyze(rc, {}))
    assert res["AMM"]["locked_pct"] == 100 and res["DLMM"]["status"] == "clean"
    b = lp_lock_breakdown(rc, res)
    assert b["locked_pct"] == 100 and b["unlockable_pct"] == 0 and b["onchain"] == 2
    # RugCheck says 100% but on-chain 40% of the LP sits in a plain wallet -> 60% locked
    res = asyncio.run(OnchainLP(FakeChain([(P, 600), (W, 400)], []), False).analyze(rc, {}))
    assert res["AMM"]["locked_pct"] == pytest.approx(60.0)
    assert lp_lock_breakdown(rc, res)["locked_pct"] == pytest.approx(60.0)
    # the dev provides the DLMM liquidity -> dev-linked pool
    res = asyncio.run(OnchainLP(FakeChain([(P, 1000)], [W]), False).analyze(rc, {W: "dev"}))
    assert res["DLMM"]["status"] == "dev" and lp_lock_breakdown(rc, res)["dev_pools"]
    # a known rugger provides it -> dev-linked too
    res = asyncio.run(OnchainLP(FakeChain([(P, 1000)], [W]), False).analyze(rc, {}, rugger=lambda w: w == W))
    assert res["DLMM"]["status"] == "dev"
    # RPC down -> no on-chain verdict, the Filter falls back to RugCheck numbers + the 50% cap
    class Down:
        async def call(self, *a):
            raise RuntimeError("429")
    res = asyncio.run(OnchainLP(Down(), False).analyze(rc, {}))
    assert "locked_pct" not in res["AMM"] and res["DLMM"]["status"] == "unverified"
    assert lp_lock_breakdown(rc, res)["locked_pct"] == 100


def test_stop_on_dexscreener_mark_is_confirmed_by_jupiter(tmp_path):
    """A lagging DexScreener price must not sell a position Jupiter still values above the stop."""
    desk = make_desk(tmp_path)
    risk = Risk(desk)
    pos = position(cost=1.0, value=0.98, peak=0.98)
    desk.portfolio.positions["M"] = pos
    risk._last_quote["M"] = time.time()          # Jupiter quoted a moment ago: not due this tick

    class Dex:
        async def snapshots(self, mints, **kw):   # DexScreener says -8% after costs
            return {"M": snap(price_native=0.00092, liquidity_usd=80_000)}

    class Jup:
        value = 0.975

        async def quote(self, i, o, amount, bps, lane=""):
            return {"outAmount": str(int(self.value * 1e9))}

    desk.dex, desk.jup = Dex(), Jup()
    asyncio.run(risk.mark_positions(time.time()))
    assert desk.exits.empty() and pos.pnl_pct == pytest.approx(-2.5)
    risk._last_quote["M"] = time.time()
    desk.jup.value = 0.93                          # Jupiter confirms -7%: sell
    asyncio.run(risk.mark_positions(time.time()))
    assert not desk.exits.empty()


def test_young_token_traction_signals():
    """Young tokens are also judged on traction: holder growth, volume pace, organic trade size, dev share."""
    web = {"url": None, "score": 0.3, "legit": 0.35, "tasty": 0.2, "flags": []}
    t = time.time()
    young = dict(pair_created_at=t - 40 * 60, volume_h1=60_000, volume_m5=9_000, buys_h1=900, sells_h1=600)
    strong = {"top10_pct": 9, "holders_total": 500, "insider_pct": 3, "risks": [], "creator_pct": 1.0}
    weak = {**strong, "holders_total": 30, "creator_pct": 14.0}
    s1, b1, why = score_token(snap(**young), strong, web)
    s2, b2, _ = score_token(snap(**young), weak, web)
    assert b1["traction"] > b2["traction"] and s1 > s2
    assert any(w.startswith("traction:") for w in why)
    # older tokens are not scored on traction
    _, b3, _ = score_token(snap(pair_created_at=t - 6 * 3600), strong, web)
    assert "traction" not in b3


# ── instant-rug guards (2026-11-10) ──
def _filter_with_rugcheck(tmp_path, top10_each: float):
    from desk.agents.filter import Filter

    class Rug:
        def __init__(self):
            self.each = top10_each

        async def report(self, mint):
            return {"token": {"supply": 1e9, "decimals": 6}, "creator": "DEV",
                    "markets": [{"marketType": "pump_fun_amm", "pubkey": "POOL",
                                 "lp": {"quoteUSD": 40_000, "baseUSD": 40_000, "lpLockedPct": 100}}],
                    "topHolders": [{"owner": f"W{i}", "pct": self.each} for i in range(10)]}

    class Rpc:
        async def mint_info(self, mint):
            return {"mintAuthority": None, "freezeAuthority": None, "decimals": 6}

    desk = make_desk(tmp_path)
    desk.rug, desk.rpc = Rug(), Rpc()
    f = Filter(desk)
    f.f.onchain_lp_check = False
    return desk, f


def test_graduation_wait_blocks_fresh_pumpswap_pairs(tmp_path):
    from desk.agents.filter import Filter
    from desk.models import Candidate
    f = Filter(make_desk(tmp_path))
    t = time.time()
    fresh = snap(pair_created_at=t - 12 * 60, liquidity_usd=30_000, volume_h1=40_000)
    wait = next(c for c in f._market_checks(fresh) if c.name == "graduation_wait")
    assert not wait.passed and not wait.permanent
    assert next(c for c in f._market_checks(snap(pair_created_at=t - 45 * 60)) if c.name == "graduation_wait").passed
    # bonding-curve pair: not graduated, no wait check
    assert not any(c.name == "graduation_wait" for c in f._market_checks(snap(dex_id="pumpfun")))
    # Only the wait fails and no recent top10 reading: deep checks still run (screen returns None)...
    assert f.screen(Candidate("Mpump"), fresh) is None
    f._top10_seen["Mpump"] = time.time()
    # ...but once a reading exists the cheap screen rejects it without RugCheck.
    rep = f.screen(Candidate("Mpump"), fresh)
    assert rep is not None and not rep.passed
    f.f.graduation_wait_minutes = 0
    assert not any(c.name == "graduation_wait" for c in f._market_checks(fresh))


def test_holder_spread_kills_after_top10_collapse(tmp_path):
    from desk.models import Candidate
    desk, f = _filter_with_rugcheck(tmp_path, 9.0)          # top10 = 90%
    rep = asyncio.run(f.deep(Candidate("M"), snap()))
    assert not rep.passed and not rep.permanent_fail       # plain top10 reject
    desk.rug.each = 0.5                                     # 10 minutes later: 5%
    rep = asyncio.run(f.deep(Candidate("M"), snap()))
    spread = next(c for c in rep.checks if c.name == "holder_spread")
    assert not spread.passed and rep.permanent_fail
    # a token that was never concentrated passes
    (tmp_path / "b").mkdir()
    _, f2 = _filter_with_rugcheck(tmp_path / "b", 0.5)
    rep2 = asyncio.run(f2.deep(Candidate("N"), snap(mint="N")))
    assert not any(c.name == "holder_spread" for c in rep2.checks) and rep2.passed


def test_rugged_name_relaunch_is_killed(tmp_path):
    from desk import rugnames
    from desk.agents.filter import Filter
    from desk.models import Candidate
    desk = make_desk(tmp_path)
    rugnames.record(desk.store, "OLDMINT", "GTA6", "GTA 6", "our trade lost -97%", creator="BADDEV")
    f = Filter(desk)
    for sym, name in (("GTA6", "Grand Theft"), ("GTA 6", "x"), ("GTA6COIN", "GTA 6 Coin")):
        # a name match alone is no longer a kill: held back until its identity is checked
        rep = f.screen(Candidate("NEW"), snap(mint="NEW", symbol=sym, name=name))
        hit = next(c for c in rep.checks if c.name == "rugged_name")
        assert not hit.passed and not hit.permanent
        # same dev wallet as the rugged token: a relaunch, killed for good
        rep = f.screen(Candidate("NEW", creator="BADDEV"), snap(mint="NEW", symbol=sym, name=name))
        hit = next(c for c in rep.checks if c.name == "rugged_name")
        assert not hit.passed and hit.permanent and "same dev" in hit.detail
    # old rule still available
    f.f.rugged_name_identity = False
    rep = f.screen(Candidate("NEW"), snap(mint="NEW", symbol="GTA6", name="x"))
    assert next(c for c in rep.checks if c.name == "rugged_name").permanent
    f.f.rugged_name_identity = True
    assert f.screen(Candidate("NEWpump"), snap(mint="NEWpump", symbol="DOGE", name="Doge")) is None
    # a name match on an otherwise clean token goes on to the deep (identity) checks
    assert f.screen(Candidate("NEWpump"), snap(mint="NEWpump", symbol="GTA6", name="GTA 6")) is None
    # old entries stop matching after rugged_name_days
    desk.store.db.execute("UPDATE rugged_names SET ts=?", (time.time() - 8 * 86400,))
    f.rugged.invalidate()
    assert f.rugged.match("NEW", "GTA6", "GTA 6") is None


def test_rugged_name_ignores_generic_words(tmp_path):
    from desk import rugnames
    desk = make_desk(tmp_path)
    rugnames.record(desk.store, "BRAIN", "\U0001f9e0", "\U0001f9e0COIN", "liquidity pulled")   # key "coin"
    rugnames.record(desk.store, "AUD", "AUDCTY", "AudaCity", "liquidity pulled")
    led = desk.store.rugged_names(0)
    assert "coin" not in led and "audacity" in led
    # a key stored before the stop-list never matches either
    desk.store.add_rugged_name("meme", "OLD", "MEME", "liquidity pulled")
    led = desk.store.rugged_names(0)
    assert rugnames.match(led, "N", "DACITY", "Dacity Coin") is None
    assert rugnames.match(led, "N", "MEME", "Meme") is None
    assert rugnames.match(led, "N", "DACITY", "GTAVI AudaCity")["symbol"] == "AUDCTY"


class _Probe:
    """SiteProbe stand-in: site -> (shows the mint?, X handles linked)."""
    def __init__(self, sites=None, meta=None):
        self.sites, self.meta = sites or {}, meta or {}

    async def metadata(self, uri):
        return self.meta.get(uri, {})

    async def telegram(self, url):
        return self.tg.get(url, {}) if hasattr(self, "tg") else {}

    async def site(self, url, mint):
        from desk.identity import own_site
        ca_mint, xs = self.sites.get(own_site(url), (None, set()))
        return {"ok": own_site(url) in self.sites, "ca": ca_mint == mint, "x": xs}


def _identity_filter(tmp_path, probe):
    from desk import rugnames
    desk, f = _filter_with_rugcheck(tmp_path, 1.0)
    rugnames.record(desk.store, "FAKEpump", "AUDCTY", "AudaCity", "liquidity pulled", creator="FAKEDEV")
    desk.store.add_token_links("FAKEpump", ["x:gtaviaudacity"])      # the fake linked the real X account
    f.site_probe = probe
    f.rugged.invalidate()
    return desk, f


def test_identity_lets_the_real_project_through(tmp_path):
    from desk.models import Candidate
    real = "REALpump"
    probe = _Probe({"gtaviaudacity.live": (real, {"gtaviaudacity"})})
    desk, f = _identity_filter(tmp_path, probe)
    s = snap(mint=real, symbol="DACITY", name="GTAVI AudaCity", websites=["https://gtaviaudacity.live"],
             socials={"twitter": "https://x.com/GTAVIAudaCity", "telegram": "https://t.me/GTAVIAudaCityPortal"})
    rep = asyncio.run(f.deep(Candidate(real), s))
    chk = next(c for c in rep.checks if c.name == "rugged_name")
    # X shared with the rugged fake, but the site shows OUR contract: the fake borrowed the real channels
    assert chk.passed, chk.detail
    assert "contract address on gtaviaudacity.live" in chk.detail and rep.passed
    assert rep.rug["identity"]["ok"]


def test_identity_blocks_copycats(tmp_path):
    from desk.models import Candidate
    probe = _Probe({"gtaviaudacity.live": ("REALpump", {"gtaviaudacity"})})
    desk, f = _identity_filter(tmp_path, probe)

    def run(mint, creator="DEV", **kw):
        desk.rug.report = _rc_with_creator(desk.rug.report, creator)
        rep = asyncio.run(f.deep(Candidate(mint), snap(mint=mint, symbol="DACITY", name="AudaCity", **kw)))
        return next(c for c in rep.checks if c.name == "rugged_name")

    # copycat linking the real site and X: the site shows the real contract, not the copycat's
    c = run("COPYpump", websites=["https://gtaviaudacity.live"], socials={"twitter": "https://x.com/GTAVIAudaCity"})
    assert not c.passed and not c.permanent
    # no channels at all
    c = run("BAREpump", websites=[], socials={})
    assert not c.passed and "no own website" in c.detail
    # only a link to somebody else's post + no profile proof
    c = run("POSTpump", websites=[], socials={"twitter": "https://x.com/solana/status/123"})
    assert not c.passed and "no own website" in c.detail
    # same dev wallet as the rugged token: relaunch, killed for good
    c = run("RELApump", creator="FAKEDEV", websites=[], socials={})
    assert not c.passed and c.permanent


def _rc_with_creator(report, creator):
    base = getattr(report, "__wrapped__", report)

    async def rep(mint):
        r = await base(mint)
        r["creator"] = creator
        return r
    rep.__wrapped__ = base
    return rep


def test_telegram_page_parsing():
    from desk.identity import parse_tg_page
    ok = parse_tg_page('<div class="tgme_page_title" dir="auto">\n  <span dir="auto">GTAVI AudaCity Portal</span>'
                       '</div><div class="tgme_page_extra">1 234 subscribers</div>')
    assert ok["exists"] and ok["kind"] == "channel" and ok["members"] == 1234
    grp = parse_tg_page('<div class="tgme_page_title"><span dir="auto">X</span></div>'
                        '<div class="tgme_page_extra">5 210 members, 300 online</div>')
    assert grp["kind"] == "group" and grp["members"] == 5210
    dead = parse_tg_page('<title>Telegram: Contact @nobody</title><div class="tgme_page_extra">If you have Telegram'
                         '</div>')
    assert not dead["exists"]


def test_identity_telegram_points(tmp_path):
    from desk.models import Candidate
    real = "REALpump"
    probe = _Probe({"realsite.xyz": (real, set())})
    probe.tg = {"https://t.me/realportal": {"ok": True, "exists": True, "kind": "channel", "members": 400},
                "https://t.me/ghost": {"ok": True, "exists": False}}
    desk, f = _identity_filter(tmp_path, probe)
    s = snap(mint=real, symbol="DACITY", name="AudaCity", websites=["https://realsite.xyz"],
             socials={"telegram": "https://t.me/realportal"})
    c = next(c for c in asyncio.run(f.deep(Candidate(real), s)).checks if c.name == "rugged_name")
    assert c.passed and "Telegram channel reachable, 400 subscribers" in c.detail     # CA 2 + profile 1 + TG 1
    s = snap(mint="GHOSTpump", symbol="DACITY", name="AudaCity", websites=[],
             socials={"twitter": "https://x.com/someone", "telegram": "https://t.me/ghost"})
    c = next(c for c in asyncio.run(f.deep(Candidate("GHOSTpump"), s)).checks if c.name == "rugged_name")
    assert not c.passed and "not visible to the bot (ignored)" in c.detail
    # a hidden Telegram never takes points away: same token without the link scores the same
    c2 = next(c for c in asyncio.run(f.deep(Candidate("GHOSTpump"), snap(
        mint="GHOSTpump", symbol="DACITY", name="AudaCity", websites=[],
        socials={"twitter": "https://x.com/someone"}))).checks if c.name == "rugged_name")
    assert c.detail.split("(identity ")[1] == c2.detail.split("(identity ")[1]


def test_identity_link_parsing():
    from desk.identity import channels, own_site, x_link
    assert x_link("https://x.com/GTAVIAudaCity") == ("account", "gtaviaudacity")
    assert x_link("https://twitter.com/Helius/status/2120161793834406166") == ("post", "helius")
    assert x_link("https://x.com/i/communities/1994900457320583239")[0] == "community"
    assert own_site("https://gtaviaudacity.live") == "gtaviaudacity.live"
    assert own_site("https://www.zoobasel.ch/de/aktuelles/news/1527/x/") is None
    assert own_site("https://tinyurl.com/y9knm4pa") is None
    assert own_site("https://pump.fun/coin/abc") is None
    ch = channels([], {"twitter": "https://x.com/x/status/1"}, {"twitter": "https://x.com/RealOne",
                                                                 "website": "realone.xyz"})
    assert ch["x"] == "realone" and ch["x_kind"] == "account" and ch["site"] == "realone.xyz"
    assert ch["profile"] is True


def test_rugged_names_seeded_from_past_trades(tmp_path):
    from desk import rugnames
    desk = make_desk(tmp_path)
    t = time.time()
    desk.store.add_trade({"mint": "R1", "symbol": "Rockstar", "mode": "paper", "entry_ts": t - 100, "exit_ts": t - 52,
                          "cost_sol": 1, "proceeds_sol": 0.02, "pnl_sol": -0.98, "pnl_pct": -98, "peak_pnl_pct": 0,
                          "score": 0.7, "entry_reason": "", "exit_reason": "", "tx_in": "", "tx_out": "", "data": {}})
    desk.store.add_trade({"mint": "W1", "symbol": "WIN", "mode": "paper", "entry_ts": t - 100, "exit_ts": t - 50,
                          "cost_sol": 1, "proceeds_sol": 0.9, "pnl_sol": -0.1, "pnl_pct": -10, "peak_pnl_pct": 0,
                          "score": 0.7, "entry_reason": "", "exit_reason": "", "tx_in": "", "tx_out": "", "data": {}})
    assert rugnames.seed_from_trades(desk.store, 60) == 1
    led = desk.store.rugged_names(0)
    assert "rockstar" in led and "win" not in led


def test_quant_caps_glitched_returns():
    from desk.quant import QuantModel
    q = QuantModel(min_samples_to_enforce=1)
    q.load([{"ts": i, "score": 0.7, "label": "up", "gross_pct": 2_032_056.0, "weight": 1.0} for i in range(5)])
    _, r, _ = q.probabilities(0.7)
    assert r["up"] <= 300


def test_graduation_wait_early_release_only_for_steady_tokens(tmp_path):
    from desk.models import Candidate
    desk, f = _filter_with_rugcheck(tmp_path, 1.0)           # top10 = 10%
    t0 = time.time()
    holders = iter([800, 1100, 1500, 1900, 2300])

    async def report(mint, base=desk.rug.report):
        r = await base(mint)
        r["totalHolders"] = next(holders)
        return r
    desk.rug.report = report
    clock = [t0]
    import desk.agents.filter as fmod

    class FakeTime:
        @staticmethod
        def time():
            return clock[0]
    fmod.time = FakeTime
    try:
        def check(grad_min):
            clock[0] = t0 + grad_min * 60
            s = snap(pair_created_at=clock[0] - grad_min * 60, liquidity_usd=30_000, volume_h1=40_000)
            import desk.models as m
            old = m.now
            m.now = lambda: clock[0]
            try:
                rep = asyncio.run(f.deep(Candidate("M"), s))
            finally:
                m.now = old
            return rep, next(c for c in rep.checks if c.name == "graduation_wait")
        _, w = check(8)                  # first reading 8 min after graduation
        assert not w.passed
        _, w = check(13)
        assert not w.passed
        _, w = check(18)                 # 3 readings over 10 min, but < 20 min since graduation
        assert not w.passed and "only 18 min" in w.detail
        rep, w = check(22)
        assert w.passed and "EARLY RELEASE" in w.detail and rep.passed
    finally:
        fmod.time = time


def test_no_early_release_if_first_seen_late_or_concentrated(tmp_path):
    from desk.models import Candidate
    desk, f = _filter_with_rugcheck(tmp_path, 1.0)
    f._wait_hist["M"] = [(time.time() - 900, 25, 5.0, 500, 30_000), (time.time() - 600, 30, 5.0, 900, 30_000),
                         (time.time() - 300, 35, 5.0, 1200, 30_000)]
    s = snap(pair_created_at=time.time() - 40 * 60, liquidity_usd=30_000, volume_h1=40_000)
    w = next(c for c in asyncio.run(f.deep(Candidate("M"), s)).checks if c.name == "graduation_wait")
    assert not w.passed and "first seen 25 min" in w.detail
    f._wait_hist["M"] = [(time.time() - 900, 10, 60.0, 500, 30_000), (time.time() - 600, 15, 5.0, 900, 30_000),
                         (time.time() - 300, 20, 5.0, 1200, 30_000)]
    w = next(c for c in asyncio.run(f.deep(Candidate("M"), s)).checks if c.name == "graduation_wait")
    assert not w.passed and "top10 was concentrated" in w.detail


def test_scout_second_look_revives_dropped_tokens(tmp_path):
    """A token dropped from the watchlist (window over / quiet) comes back when it trades again;
    a killed token never does, and dust is not archived at all."""
    from desk.agents.scout import Scout, Watch
    from desk.models import Candidate

    desk = make_desk(tmp_path)
    sc = Scout(desk)
    t = time.time()
    old = t - (sc.c.watch_minutes + 5) * 60
    sc.watch["A"] = Watch(Candidate("A", "AAA", source="pumpfun:new"), added=old, max_liq=40_000, first_seen=old)
    sc.watch["K"] = Watch(Candidate("K", "KKK", source="pumpfun:new"), added=old, max_liq=40_000, first_seen=old)
    sc.watch["D"] = Watch(Candidate("D", "DST", source="pumpfun:new"), added=old, max_liq=800, first_seen=old)
    desk.blacklist["K"] = "rugged"
    sc._prune()
    assert not sc.watch and set(sc.archive) == {"A"}

    quiet = {"A": snap(mint="A", volume_h1=3_000, liquidity_usd=30_000)}
    assert sc.revive_due(quiet) == [] and "A" in sc.archive
    live = {"A": snap(mint="A", volume_h1=60_000, liquidity_usd=30_000)}
    assert sc.revive_due(live) == ["A"]
    w = sc.watch["A"]
    assert w.cand.source == "revisit:pumpfun:new" and w.revived == 1 and w.first_seen == old
    # killed after the revival: dropped and never archived again
    desk.blacklist["A"] = "holder spread"
    sc._prune()
    assert "A" not in sc.watch and "A" not in sc.archive


def test_scout_graduation_restarts_watch_window(tmp_path):
    from desk.agents.scout import Scout, Watch
    from desk.models import Candidate

    desk = make_desk(tmp_path)
    sc = Scout(desk)
    old = time.time() - 170 * 60
    sc.watch["G"] = Watch(Candidate("G", "GRD", source="pumpfun:new"), added=old, first_seen=old)
    sc.discover("G", "GRD", "", "pumpfun:migration", urgent=True)
    assert time.time() - sc.watch["G"].added < 5 and sc.watch["G"].first_seen == old


# ── no stop at a loss (user's rule 2026-11-11, paper and live) ──
@pytest.mark.parametrize("mode", ["paper", "live"])
def test_no_loss_stop_config(mode):
    cfg = Config()
    cfg.mode = mode
    cfg.risk.loss_stop = False
    cfg.risk.paper_hard_stop_pct = -25.0
    cfg.risk.max_hold_minutes = 45
    enforce_hard_limits(cfg)
    assert cfg.risk.exit_stop_pct == -100.0
    assert cfg.risk.max_hold_minutes == 0
    assert cfg.risk.hard_stop_pct == -6.0         # the QUANT label barrier is unchanged


def test_no_loss_stop_holds_losers_but_keeps_profit_and_rug_exits():
    cfg = Config()
    cfg.risk.loss_stop = False
    enforce_hard_limits(cfg)
    r = cfg.risk
    t = time.time()
    for v in (1.0, 0.94, 0.70, 0.30, 0.02):        # break-even down to -98%: no exit
        assert exit_decision(position(1.0, v), r, t, 80_000)[0] is None
    old = position(1.0, 0.80)
    old.entry_ts = t - 6 * 3600                     # held for hours at a loss: no time exit
    assert exit_decision(old, r, t, 80_000)[0] is None
    p = position(1.0, 1.11, peak=1.11)              # profit lock still arms and sells in profit
    p.entry_ts = t - 600
    assert exit_decision(p, r, t, 80_000)[0] is None and p.lock_armed_on
    p.mark_value_sol = 1.04
    assert exit_decision(p, r, t, 80_000)[0] == "PROFIT_LOCK"
    p = position(1.0, 1.40, peak=1.40)              # trailing still sells
    exit_decision(p, r, t, 80_000)
    p.mark_value_sol = 1.40 * 0.90
    assert exit_decision(p, r, t, 80_000)[0] in ("TRAILING_STOP", "PROFIT_LOCK")
    assert exit_decision(position(1.0, 0.7), r, t, 40_000)[0] == "LIQUIDITY_PULL"   # rug: pool pulled
    r.liquidity_drop_exit_pct = 0                   # user's choice: liquidity exit off too
    assert exit_decision(position(1.0, 0.7), r, t, 1_000)[0] is None


@pytest.mark.parametrize("loss_stop", [False, True])
def test_profit_lock_is_never_off(loss_stop):
    cfg = Config()
    cfg.risk.loss_stop = loss_stop
    cfg.risk.profit_lock_activate_pct = 0          # what the Controller applied on 2026-11-11 19:41 UTC
    enforce_hard_limits(cfg)
    assert cfg.risk.profit_lock_activate_pct == 8.0
    assert any("profit lock can never be turned off" in w for w in cfg.warnings)
