"""2026-11-16 (user): the review of the agents' work, points 1-8 — real horizon, QUANT measure-only, entry liquidity
readings, positions that no longer block the limit, the Analyst skipping held tokens, the Controller within the
user's rules, the Whale checker measured from the signal price."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace as NS

from desk import realwatch as rwm
from desk import realreport as rr
from desk.config import Config, enforce_hard_limits
from desk.controller import build_spaces, evaluate_space, parse_rows, real_rows_only
from desk.entry import EntryPlan
from desk.quant import QuantModel, learning_rows, model_from_cfg, trade_label
from tests.test_rules import make_desk, position, snap

H = 3600.0


def user_cfg(cfg: Config | None = None) -> Config:
    """The user's rules as in config.toml: no stop at a loss, profit stop from +30%, 60% / 70% of the peak."""
    cfg = cfg or Config()
    r = cfg.risk
    r.loss_stop = False
    r.trail_activate_pct, r.trail_distance_pct, r.max_hold_minutes = 50.0, 0.0, 0
    r.profit_lock_activate_pct = r.profit_lock_fast_activate_pct = 30.0
    r.profit_lock_pct, r.profit_lock_keep_from_pct, r.profit_lock_keep_pct = 4.0, 20.0, 70.0
    r.profit_lock_keep_fast_pct, r.profit_lock_keep_low_pct, r.profit_lock_keep_high_from_pct = 70.0, 60.0, 50.0
    r.profit_lock_fee_multiple = 1.0
    cfg.quant.learn_since = ""
    enforce_hard_limits(cfg)
    return cfg


# ── the real horizon ──
def test_outcome_and_judge_on_the_whole_path():
    cfg = user_cfg()
    up = [(0, 0.0), (600, 20.0), (1200, 40.0), (1800, 45.0), (2400, 20.0)]     # +40% net, back to +18% -> sold
    j = rwm.judge(up, 2.0, cfg)
    assert j["label"] == "up" and j["sold_s"] == 2400 and 15 < j["ret_net"] < 30
    slow_death = [(0, 0.0), (1800, 10.0), (3600, -10.0), (6 * 3600, -60.0), (20 * 3600, -90.0)]
    j = rwm.judge(slow_death, 2.0, cfg)
    assert j["label"] == "down" and j["sold_s"] is None and j["ret_net"] < -90    # held, never sold at a loss
    # 60 minutes alone would have said "neither, -12%": the real horizon is what changes the verdict
    assert rwm.outcome(slow_death[:3], 2.0, 30.0, -50.0) == "neither"


def _shadow_row(mint="Apump", stage="analyst", decision="approve", score=0.7, start=None):
    return {"mint": mint, "symbol": mint[:3], "stage": stage, "decision": decision, "score": score, "traded": 0,
            "start_ts": start if start is not None else time.time() - 2 * H, "ref_price": 0.001,
            "ref_liq": 30_000.0, "cost_pct": 2.0, "features": json.dumps({"mcap": 120_000, "age_min": 40})}


def test_realwatch_follows_after_the_hour_and_closes(tmp_path):
    desk = make_desk(tmp_path)
    user_cfg(desk.cfg)
    desk.quant = model_from_cfg(desk.cfg)
    w = rwm.RealWatch(desk)
    t0 = time.time() - 25 * H
    assert w.start(_shadow_row("Apump", start=t0), [(0, 0.0), (3600, 5.0)])
    assert w.start(_shadow_row("Bpump", start=t0), [(0, 0.0), (3600, 3.0)])
    assert w.start(_shadow_row("Gpump", "grad45", "buy_at_wait_end", None, start=t0), [(0, 0.0)])
    assert not w.start(_shadow_row("Xpump", "filter", "reject"), [])           # not our kind of row
    assert w.active_count() == 3
    rows = w.due(now=t0 + 2 * H)
    assert len(rows) == 3
    # A pumps to +50% and is sold by the profit stop -> closed at once ("deciso"); B's pool is pulled
    w.update({"Apump": snap(mint="Apump", price_usd=0.0015, liquidity_usd=40_000),
              "Bpump": snap(mint="Bpump", price_usd=0.0009, liquidity_usd=1_000),
              "Gpump": snap(mint="Gpump", price_usd=0.0011, liquidity_usd=30_000)}, rows, now=t0 + 2 * H)
    w.update({"Apump": snap(mint="Apump", price_usd=0.0011, liquidity_usd=40_000),
              "Gpump": snap(mint="Gpump", price_usd=0.0011, liquidity_usd=30_000)},
             w.due(now=t0 + 3 * H), now=t0 + 3 * H)
    a = desk.store.q("SELECT * FROM realwatch WHERE mint='Apump'")[0]
    b = desk.store.q("SELECT * FROM realwatch WHERE mint='Bpump'")[0]
    assert a["done"] == 1 and a["label"] == "up" and a["end_reason"] == "deciso" and a["ret_net"] > 0
    assert b["done"] == 1 and b["label"] == "down" and b["end_reason"] == "pool svuotata" and b["ret_net"] <= -100
    # G: window over after 24 h
    assert w.update({"Gpump": snap(mint="Gpump", price_usd=0.0012, liquidity_usd=30_000)},
                    w.due(now=t0 + 24.1 * H), now=t0 + 24.1 * H) == 1
    g = desk.store.q("SELECT * FROM realwatch WHERE mint='Gpump'")[0]
    assert g["done"] == 1 and g["label"] == "neither" and g["end_reason"] == "fine finestra"
    # QUANT learned the two Analyst rows (real outcomes), not the graduation-wait one
    assert sorted(o["label"] for o in desk.quant.outcomes) == ["down", "up"]
    assert all(o["source"] == "real" for o in desk.quant.outcomes)
    # and at the next start it loads them again, instead of the 60-minute rows
    rows = learning_rows([desk.store], desk.cfg)
    assert sorted(o["label"] for o in rows if o["source"] == "real") == ["down", "up"]


def test_shadow_hands_its_rows_to_the_real_horizon(tmp_path):
    from desk.shadow import Shadow, ShadowTracker
    desk = make_desk(tmp_path)
    user_cfg(desk.cfg)
    desk.quant = model_from_cfg(desk.cfg)
    desk.realwatch = rwm.RealWatch(desk)
    st = ShadowTracker(desk, lambda *a, **k: None)
    s = Shadow("Apump:analyst", "Apump", "A", "analyst", "approve", "", 0.7, 0.001, 30_000, 2.0, time.time() - H,
               features={"mcap": 1})
    s.path = [(0, 0.0), (3600, 4.0)]
    s.desk_label, s.model_label, s.sim_ret = "neither", "neither", 2.0
    st._store(s)
    assert desk.realwatch.active_count() == 1
    assert not desk.quant.outcomes            # the 60-minute row is no longer a QUANT sample: the 24 h one will be


def test_trades_and_old_positions_are_labelled_on_the_real_result(tmp_path):
    desk = make_desk(tmp_path)
    cfg = user_cfg(desk.cfg)
    assert trade_label({"label": "up", "ret_net_pct": -60.0}, cfg) == "down"
    assert trade_label({"label": "down", "ret_net_pct": -20.0}, cfg) == "neither"
    assert trade_label({"label": "neither", "ret_net_pct": 22.0}, cfg) == "up"
    p = position(cost=1.0, value=0.2)
    p.mint, p.entry_ts = "Zpump", time.time() - 30 * H
    desk.store.save_position(p.mint, {**p.__dict__})
    held = [o for o in learning_rows([desk.store], cfg) if o["source"] == "held"]
    assert len(held) == 1 and held[0]["label"] == "down" and round(held[0]["ret_net_pct"]) == -80


# ── QUANT measures only ──
def test_quant_only_measures_by_default():
    cfg = user_cfg()
    assert cfg.quant.enforce is False
    q = model_from_cfg(cfg)
    q.min_samples_to_enforce = 10
    q.load([{"ts": i, "score": 0.7, "label": "down", "gross_pct": -100.0, "ret_net_pct": -100.0, "weight": 1.0}
            for i in range(40)])
    v = q.evaluate(0.7, 2.0)
    assert v.ev_pct < 0 and v.passes and q.blocked == 0 and "solo misura" in q.status()
    q.enforce = True
    assert not q.evaluate(0.7, 2.0).passes                      # the user can switch the gate back on


# ── point 1: a rugged-name block never expires by time ──
def test_name_block_rules_are_hard_limits():
    cfg = Config()
    cfg.filter.rugged_name_expire_minutes, cfg.filter.identity_min_score = 30, 1
    enforce_hard_limits(cfg)
    assert cfg.filter.rugged_name_expire_minutes == 0 and cfg.filter.identity_min_score == 3


# ── point 4: entry liquidity readings ──
def test_one_low_liquidity_reading_does_not_cancel():
    p = EntryPlan(mode="dip", window_s=300, dip_pct=3, liq_confirm=2)
    assert p.update(5, -4.0, 0.70) == "wait"          # low reading: no buy on it, no cancel either
    assert p.update(10, -1.0, 1.02) == "wait"         # liquidity is fine again: the count restarts
    assert p.update(15, -0.5, 0.75) == "wait"
    assert p.update(20, -0.5, 0.74) == "cancel" and "2 letture" in p.why
    p = EntryPlan(mode="dip", window_s=300, dip_pct=3, liq_confirm=2)
    assert p.update(5, -1.0, 0.70) == "wait" and p.update(10, -4.0, 0.99) == "buy"


# ── point 5: dead positions don't block the limit (and are never sold for it) ──
def test_zombie_positions_do_not_count_toward_the_limit(tmp_path):
    from desk.agents.risk import Risk, is_zombie
    desk = make_desk(tmp_path)
    cfg = desk.cfg
    cfg.executor.max_open_positions = 2
    old_dead = position(cost=1.0, value=0.2)
    old_dead.mint, old_dead.entry_ts = "Dead", time.time() - 13 * H
    young_dead = position(cost=1.0, value=0.2)
    young_dead.mint = "Young"
    old_ok = position(cost=1.0, value=0.9)
    old_ok.mint, old_ok.entry_ts = "Ok", time.time() - 20 * H
    assert is_zombie(old_dead, cfg.executor) and not is_zombie(young_dead, cfg.executor)
    assert not is_zombie(old_ok, cfg.executor)
    risk = Risk(desk)
    desk.portfolio.positions = {"Dead": old_dead, "Young": young_dead}
    assert risk.approve_entry("New") == (True, "approved")
    desk.portfolio.positions["Ok"] = old_ok
    ok, why = risk.approve_entry("New")
    assert not ok and "1 ferme in perdita" in why
    cfg.executor.zombie_hours = 0
    assert not is_zombie(old_dead, cfg.executor)


def test_analyst_skips_tokens_risk_would_refuse(tmp_path):
    from desk.agents.analyst import Analyst
    desk = make_desk(tmp_path)
    a = Analyst(desk)
    desk.portfolio.positions = {"Held": position()}
    desk.risk_history = {"Sold": {"ts": time.time() - 3600, "pnl_sol": 0.1},
                         "Lost": {"ts": time.time() - 30 * H, "pnl_sol": -0.1},
                         "Long": {"ts": time.time() - 30 * H, "pnl_sol": 0.1}}
    assert a.held_reason("Held") and a.held_reason("Sold") and a.held_reason("Lost")
    assert not a.held_reason("Long") and not a.held_reason("New")


# ── point 3: the Controller on the real horizon, within the user's rules ──
def _raw(i, path, score=0.7, real=True):
    f = {"mcap": 100_000 + 10_000 * i, "age_min": 30 + i, "liq_usd": 30_000, "score": score}
    return {"start_ts": 1_000_000 + i, "mint": f"M{i}pump", "score": score, "decision": "approve", "sim_net_pct": 0.0,
            "cost_pct": 2.0, "features": json.dumps(f), "path": json.dumps(path), **({"real": 1} if real else {})}


def test_controller_uses_only_real_rows_once_there_are_enough():
    cfg = user_cfg()
    cfg.realwatch.min_rows = 3
    raw = [_raw(i, [[0, 0], [600, 5]], real=i < 3) for i in range(6)]
    rows, info = real_rows_only(raw, cfg)
    assert info["use_real"] and len(rows) == 3
    cfg.realwatch.min_rows = 5
    rows, info = real_rows_only(raw, cfg)
    assert not info["use_real"] and len(rows) == 6


def test_risk_tests_never_go_below_the_users_profit_stop():
    cfg = user_cfg()
    cfg.controller.min_rows = 10
    paths = [[[0, 0], [300, 15], [600, 35], [900, 10]], [[0, 0], [300, 40], [600, 25]]]
    rows = parse_rows([_raw(i, paths[i % 2]) for i in range(40)], 240, cfg)
    sp = next(s for s in build_spaces(cfg, rows, [], {}, 10) if s.agent == "Risk")
    old = {**sp.current, "profit_lock_activate_pct": 12.0, "profit_lock_fast_activate_pct": 12.0,
           "trail_activate_pct": 40.0, "trail_distance_pct": 8.0}
    assert not sp.valid(old)
    import random
    res, _ = evaluate_space(sp, random.Random(1), 30, [old], 0.3, 10)
    for r in res:
        v = r.get("variant")
        if v:
            assert v["profit_lock_activate_pct"] >= 30 and v["trail_distance_pct"] == 0


def test_real_report_sections():
    cfg = user_cfg()
    rows = []
    for i in range(40):
        up = i % 3 == 0
        path = [[0, 0], [600, 40 if up else -10], [3000, 20 if up else -70]]
        rows.append({"mint": f"M{i}pump", "stage": "analyst", "score": 0.7 if i % 2 else 0.5, "start_ts": i,
                     "cost_pct": 2.0, "path": json.dumps(path),
                     "features": json.dumps({"mcap": 300_000 if i % 4 == 0 else 80_000, "age_min": 900 if i % 5 == 0
                                             else 30, "c_website": i / 40, "c_buy_pressure": 1 - i / 40})})
    j = rr.judged(rows, cfg)
    txt = rr.section(j, [], cfg)
    assert "Orizzonte reale" in txt and "oltre $250k" in txt and "oltre 12 ore" in txt and "sito web" in txt


def test_whale_gain_is_measured_from_the_signal_price():
    from desk import whale_audit as wa
    real = [{"mint": "W1", "path": [(0, 0), (60, 40)], "cost": 2.0}, {"mint": "W2", "path": [(0, 0), (60, -60)],
                                                                         "cost": 2.0},
            {"mint": "N1", "path": [(0, 0), (60, -60)], "cost": 2.0}]
    g = wa.gain_stats(real, {"W1", "W2"}, 30.0, -50.0)
    assert g["with"] == {"n": 2, "up": 1, "share": 0.5} and g["without"]["up"] == 0
