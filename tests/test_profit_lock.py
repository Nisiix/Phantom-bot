"""Profit lock: after about +5/+6% net the stop moves to about +2.5% net, sale costs included."""
from __future__ import annotations

import time

from desk.agents.risk import exit_decision, exit_slip
from desk.config import Config, RiskCfg, enforce_hard_limits
from desk.controller import simulate_exit
from desk.exits import lock_rule, lock_step
from desk.models import Position
from desk.shadow import Shadow, update_shadow


def pos(value: float, peak: float, age_s: float) -> Position:
    t = time.time()
    return Position(mint="M", symbol="TST", tokens=1000, decimals=6, cost_sol=1.0, entry_price_sol=0.001,
                    entry_ts=t - age_s, entry_liquidity_usd=80_000, exit_cost_pct=1.0, score=0.7,
                    entry_reason="test", mark_value_sol=value, peak_value_sol=peak)


def test_slow_move_arms_at_5_net_plus_sale_costs():
    r = RiskCfg()                                  # +5% / +6% fast (3 min) -> +2.5%, 1% sale buffer
    p = pos(1.055, 1.055, age_s=600)               # +5.5% marked: under 5 + 1 buffer, not armed
    assert exit_decision(p, r, time.time(), 80_000)[0] is None and not p.lock_armed_on
    p.mark_value_sol = p.peak_value_sol = 1.061    # +6.1% after 10 min: armed at the +5% net level
    assert exit_decision(p, r, time.time(), 80_000)[0] is None and p.lock_armed_on == 5.0
    p.mark_value_sol = 1.04                        # +4%: above the +3.5% marked sell level
    assert exit_decision(p, r, time.time(), 80_000)[0] is None
    p.mark_value_sol = 1.034                       # +3.4% marked = +2.4% after the sale costs -> sell
    reason, detail = exit_decision(p, r, time.time(), 80_000)
    assert reason == "PROFIT_LOCK" and "+2.5% net" in detail


def test_fast_move_gets_more_room():
    r = RiskCfg()
    p = pos(1.065, 1.065, age_s=60)                # +6.5% one minute after the buy: fast move, needs +6% +1
    assert exit_decision(p, r, time.time(), 80_000)[0] is None and not p.lock_armed_on
    p.mark_value_sol = 1.03                        # back to +3%: not armed, so no profit-lock sale
    assert exit_decision(p, r, time.time(), 80_000)[0] is None
    p.mark_value_sol = p.peak_value_sol = 1.071
    exit_decision(p, r, time.time(), 80_000)
    assert p.lock_armed_on == 6.0


def test_measured_slippage_raises_every_level():
    r = RiskCfg()
    lk = lock_rule(r, buffer=2.0)                  # sales lost 2% vs the trigger: levels go up by 2
    p = pos(1.065, 1.065, age_s=600)
    assert exit_decision(p, r, time.time(), 80_000, lk)[0] is None and not p.lock_armed_on
    p.mark_value_sol = p.peak_value_sol = 1.071
    exit_decision(p, r, time.time(), 80_000, lk)
    assert p.lock_armed_on == 5.0 and p.lock_buffer == 2.0
    p.mark_value_sol = 1.044                       # +4.4% marked <= 2.5 + 2 -> sell
    assert exit_decision(p, r, time.time(), 80_000, lk)[0] == "PROFIT_LOCK"


def test_trailing_still_wins_above_the_lock():
    r = RiskCfg(trail_activate_pct=25, trail_distance_pct=8)
    p = pos(1.30, 1.30, age_s=600)
    exit_decision(p, r, time.time(), 80_000)
    p.mark_value_sol = 1.30 * 0.91
    assert exit_decision(p, r, time.time(), 80_000)[0] == "TRAILING_STOP"


def test_lock_off_and_hard_limits():
    cfg = Config()
    cfg.risk.profit_lock_activate_pct = 0
    enforce_hard_limits(cfg)
    assert not lock_rule(cfg.risk).enabled
    cfg = Config()
    cfg.risk.profit_lock_pct = -1.0                # a "lock" at a loss is never allowed
    cfg.risk.profit_lock_sell_buffer_pct = 0.0     # nor a lock that ignores the sale costs
    enforce_hard_limits(cfg)
    assert cfg.risk.profit_lock_pct == 2.5 and cfg.risk.profit_lock_sell_buffer_pct == 1.0
    cfg = Config()
    cfg.risk.profit_lock_pct = 5.0                 # must sell at least 1 point under the arming level
    enforce_hard_limits(cfg)
    assert cfg.risk.profit_lock_pct == 2.5


def test_lock_step_pure():
    lk = lock_rule(RiskCfg())
    touch, armed = lock_step(lk, 6.2, 400, None, 0.0)
    assert touch == 400 and armed == 5.0
    touch, armed = lock_step(lk, 6.2, 100, None, 0.0)   # fast: needs 7
    assert touch == 100 and armed == 0.0
    assert lock_step(lk, 7.1, 150, touch, armed)[1] == 6.0


def test_controller_and_shadow_replay_the_lock():
    lk = lock_rule(RiskCfg())
    path = [(300, 2.0), (600, 9.0), (900, 5.0), (1200, -30.0)]   # cost 2: net 0, +7, +3, -32
    assert simulate_exit(path, 2.0, -25, 25, 8, 45) == -32.0
    assert simulate_exit(path, 2.0, -25, 25, 8, 45, lk) == 3.0
    s = Shadow(key="M", mint="M", symbol="T", stage="analyst", decision="approve", reason="", score=0.7, start=0.0,
               ref_price=1.0, ref_liq=1.0, cost_pct=2.0)
    for t, px in ((300, 1.02), (600, 1.09), (900, 1.05), (1200, 0.70)):
        update_shadow(s, px, t, -4, 25, 8, 45, -25, lk)
    assert not s.sim_open and abs(s.sim_ret - 3.0) < 1e-9


def test_exit_record_and_slippage():
    data = {"exit": {"kind": "PROFIT_LOCK", "trigger_pnl_pct": 3.4, "realized_pnl_pct": 2.6}}
    assert abs(exit_slip(data) - 0.8) < 1e-9
    assert exit_slip({"exit": {"kind": "HARD_STOP", "trigger_pnl_pct": -25, "realized_pnl_pct": -90}}) is None
    assert exit_slip("not json") is None


def test_lock_is_proportional_to_the_peak():
    r = RiskCfg(trail_activate_pct=25, trail_distance_pct=8)   # keeps 50% of the net peak, min +2.5%
    p = pos(1.21, 1.21, age_s=600)                 # +21% marked = +20% net after the sale costs
    exit_decision(p, r, time.time(), 80_000)
    p.mark_value_sol = 1.12                        # +12% marked: still above 50% x 20 = +10% net (+11% marked)
    assert exit_decision(p, r, time.time(), 80_000)[0] is None
    p.mark_value_sol = 1.105                       # +10.5% marked -> sell, keeping about +10% net
    reason, detail = exit_decision(p, r, time.time(), 80_000)
    assert reason == "PROFIT_LOCK" and "+10.0% net" in detail


def test_fixed_lock_variant():
    r = RiskCfg(profit_lock_keep_pct=0, trail_activate_pct=25)   # the plain break-even-plus-fees lock
    p = pos(1.21, 1.21, age_s=600)
    exit_decision(p, r, time.time(), 80_000)
    p.mark_value_sol = 1.04                        # far below the peak but above +2.5 + 1: hold
    assert exit_decision(p, r, time.time(), 80_000)[0] is None
    p.mark_value_sol = 1.035
    assert exit_decision(p, r, time.time(), 80_000)[0] == "PROFIT_LOCK"


def test_controller_picks_lock_levels_only_if_no_less_safe():
    from desk.controller import no_less_safe
    base = [5.0, -4.0, 3.0, 10.0, -25.0, 2.0, 1.0, 4.0, 6.0, 8.0]
    idx = list(range(len(base)))
    better_safer = [5.0, 3.0, 3.0, 10.0, -25.0, 2.0, 1.0, 4.0, 6.0, 8.0]     # a loser turned into a win
    richer_riskier = [9.0, -4.0, -1.0, 30.0, -25.0, 2.0, 1.0, 4.0, 6.0, 8.0]  # more profit, one more loser
    assert no_less_safe(better_safer, base, idx)
    assert not no_less_safe(richer_riskier, base, idx)


def test_fee_rule_waits_until_profit_covers_the_fees():
    from desk.exits import arm_level
    lk = lock_rule({"profit_lock_activate_pct": 7, "profit_lock_fast_activate_pct": 7, "profit_lock_pct": 4,
                    "profit_lock_fee_multiple": 2}, buffer=1.0)
    assert arm_level(lk, 2.0) == 7.0               # cheap fees: the plain +7% level
    assert arm_level(lk, 5.0) == 10.0              # 5% fees: needs 2 x 5 = +10% net
    assert lock_step(lk, 9.0, 600, None, 0.0, cost=5.0)[1] == 0.0
    assert lock_step(lk, 11.1, 600, None, 0.0, cost=5.0)[1] == 10.0
    assert lock_step(lk, 8.1, 600, None, 0.0, cost=2.0)[1] == 7.0


def test_risk_uses_the_trade_fees():
    r = RiskCfg(profit_lock_activate_pct=7, profit_lock_fast_activate_pct=7, profit_lock_pct=4,
                profit_lock_keep_pct=0, profit_lock_fee_multiple=2)
    p = pos(1.09, 1.09, age_s=600)
    p.rt_cost_pct = 5.0                            # expensive token: +9% marked is not enough (needs 10 + 1)
    assert exit_decision(p, r, time.time(), 80_000)[0] is None and not p.lock_armed_on
    p.mark_value_sol = p.peak_value_sol = 1.111
    exit_decision(p, r, time.time(), 80_000)
    assert p.lock_armed_on == 10.0
    p.mark_value_sol = 1.049                       # back under +4% net + 1% sale costs -> sell
    assert exit_decision(p, r, time.time(), 80_000)[0] == "PROFIT_LOCK"
