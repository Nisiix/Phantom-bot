"""QUANT (EV after costs + calibration) and Shadow tracker tests."""
from __future__ import annotations

import asyncio

import pytest

from desk.config import Config, enforce_hard_limits
from desk.quant import QuantModel, bucket_of, label_outcome
from desk.shadow import Shadow, finalize_shadow, shadow_to_outcome, update_shadow
from tests.test_rules import make_desk, signed_verdict, snap
from desk.agents.executor import Executor
from desk.agents.risk import Risk
from desk.brokers.paper import PaperBroker

H = 3600.0


def outcome(label, gross, score=0.65, weight=1.0):
    return {"ts": 0, "score": score, "label": label, "gross_pct": gross, "ret_net_pct": gross - 2,
            "p_up": 0.35, "p_down": 0.45, "p_neither": 0.2, "weight": weight}


# ── labels ──
def test_label_first_barrier_wins():
    assert label_outcome(0, 500, 100, 200, 5, H, -6) == "up"
    assert label_outcome(0, 500, 300, 200, -6, H, -6) == "down"
    assert label_outcome(0, 7200, 4000, 0, 8, H, -6) == "neither"      # +12 only after 60 min
    assert label_outcome(0, 900, 0, 0, -40, H, -6) == "down"           # gapped through the stop
    assert label_outcome(0, 900, 0, 0, -2, H, -6) == "neither"


# ── QUANT model ──
def test_warmup_never_blocks():
    q = QuantModel(min_samples_to_enforce=30)
    v = q.evaluate(0.65, cost_pct=50)        # absurd cost, but still warm-up
    assert v.ev_pct < 0 and v.passes and not v.enforcing


def test_blocks_negative_edge_and_allows_positive_edge():
    bad = QuantModel(min_samples_to_enforce=30)
    bad.load([outcome("down", -7) for _ in range(30)] + [outcome("up", 12) for _ in range(5)])
    v = bad.evaluate(0.65, cost_pct=2)
    assert v.enforcing and not v.passes and "negative edge" in v.reason
    good = QuantModel(min_samples_to_enforce=30)
    good.load([outcome("up", 18) for _ in range(25)] + [outcome("down", -7) for _ in range(10)])
    assert good.evaluate(0.65, cost_pct=2).passes


def test_cost_is_subtracted():
    q = QuantModel()
    q.load([outcome("up", 15) for _ in range(20)] + [outcome("down", -7) for _ in range(20)])
    assert q.evaluate(0.65, 1).ev_pct - q.evaluate(0.65, 4).ev_pct == pytest.approx(3.0)


def test_probabilities_sum_to_one_and_follow_data():
    q = QuantModel()
    q.load([outcome("up", 15, score=0.85) for _ in range(40)] + [outcome("down", -7, score=0.62) for _ in range(40)])
    hi, _, _ = q.probabilities(0.85)
    lo, _, _ = q.probabilities(0.62)
    assert sum(hi.values()) == pytest.approx(1) and sum(lo.values()) == pytest.approx(1)
    assert hi["up"] > lo["up"] and lo["down"] > hi["down"]


def test_shadow_samples_count_less():
    q = QuantModel(min_samples_to_enforce=30)
    q.load([outcome("down", -7, weight=0.5) for _ in range(40)])
    assert q.effective_samples == 20 and not q.enforcing


def test_buckets_cover_full_range():
    assert bucket_of(0.1) == 0 and bucket_of(0.55) == 1 and bucket_of(0.6) == 2 and bucket_of(1.0) == 5


def test_quant_config_cannot_loosen():
    cfg = Config()
    cfg.quant.min_ev_pct = -5
    enforce_hard_limits(cfg)
    assert cfg.quant.min_ev_pct == 0


def test_executor_respects_quant_block(tmp_path):
    desk = make_desk(tmp_path)
    desk.quant = QuantModel(min_samples_to_enforce=30)
    desk.quant.load([outcome("down", -8, score=0.72) for _ in range(40)])

    class FakeJup:
        async def quote(self, i, o, amount, bps):
            if i.startswith("So111"):
                return {"inAmount": str(amount), "outAmount": str(int(amount / 1e9 / 0.00001 * 1e6))}
            return {"outAmount": str(int(int(amount) * 0.00001 / 1e6 * 1e9 * 0.98))}

    class FakeDex:
        async def snapshot(self, mint, fresh=True):
            return snap(mint=mint)

    desk.jup, desk.dex = FakeJup(), FakeDex()
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6
    asyncio.run(ex.handle_verdict(v))
    assert "M" not in desk.portfolio.positions
    assert ex.state.counters.get("ev_block") == 1


# ── shadow tracker ──
def shadow(cost=2.0):
    return Shadow("k", "M", "TST", "analyst", "reject_score", "r", 0.55, 1.0, 50_000, cost, 0.0)


def test_shadow_desk_and_model_barriers():
    s = shadow()
    for t, px in enumerate([1.05, 1.15, 1.30, 1.65, 1.2]):
        update_shadow(s, px, t, -6, 12, 6)
    finalize_shadow(s)
    assert s.desk_label == "up" and s.desk_hit_ts == 1       # +15% raw = +13% net
    assert s.model_label == "up" and s.model_hit_ts == 3     # +65% raw
    assert s.sim_trail and s.sim_ret is not None and s.sim_ret > 5  # trailing exit locked a gain


def test_shadow_stop_uses_net_return():
    s = shadow(cost=2.0)
    update_shadow(s, 0.955, 1, -6, 12, 6)   # -4.5% raw = -6.5% net -> desk stop
    update_shadow(s, 0.65, 2, -6, 12, 6)
    finalize_shadow(s)
    assert s.desk_label == "down" and s.model_label == "down"
    assert s.sim_ret == pytest.approx(-6.5)


def test_shadow_neither_and_outcome_row():
    s = shadow()
    update_shadow(s, 1.03, 1, -6, 12, 6)
    finalize_shadow(s)
    assert s.desk_label == "neither" and s.model_label == "neither"
    row = {"end_ts": 1, "mode": "paper", "mint": "M", "symbol": "TST", "score": 0.55, "desk_label": "neither",
           "sim_net_pct": s.sim_ret, "cost_pct": 2.0, "p_up": 0.3, "p_down": 0.5, "p_neither": 0.2}
    o = shadow_to_outcome(row, 0.5)
    assert o["weight"] == 0.5 and o["gross_pct"] == pytest.approx(3.0) and o["label"] == "neither"


# ── QUANT judges each token on comparable tokens (fix 2026-11-11) ──
def test_rejected_tokens_do_not_poison_approved_ones():
    rows = [outcome("down", -90, score=0.45) for _ in range(60)]                      # rejected rugs
    rows += [outcome("up", 20, score=0.70) for _ in range(25)] + [outcome("down", -8, score=0.70) for _ in range(15)]
    old = QuantModel(min_samples_to_enforce=30)
    old.load(rows)
    new = QuantModel(min_samples_to_enforce=30, min_score=0.65)
    new.load(rows)
    assert not old.evaluate(0.70, 2).passes            # pooled: the rejected rugs made every token negative
    v = new.evaluate(0.70, 2)
    assert v.enforcing and v.passes and v.ev_pct > 0
    assert not new.evaluate(0.45, 2).passes            # a low-score token is still judged on its peers


def test_payoffs_follow_the_score_bucket():
    q = QuantModel(min_score=0.6)
    q.load([outcome("up", 40, score=0.85) for _ in range(30)] + [outcome("up", 2, score=0.62) for _ in range(30)])
    _, r_hi, _ = q.probabilities(0.85)
    _, r_lo, _ = q.probabilities(0.62)
    assert r_hi["up"] > r_lo["up"] + 15


def test_enforcing_counts_only_approved_outcomes():
    q = QuantModel(min_samples_to_enforce=30, min_score=0.65)
    q.load([outcome("down", -7, score=0.40) for _ in range(100)] + [outcome("up", 15, score=0.7) for _ in range(5)])
    assert q.effective_samples == 5 and not q.enforcing


def _cfg():
    cfg = Config()
    cfg.scout.venues = "pumpfun"
    cfg.quant.learn_since = "2026-11-10T09:03:00Z"
    cfg.risk.hard_stop_pct, cfg.risk.trail_activate_pct, cfg.risk.trail_distance_pct = -4, 25, 8
    cfg.risk.max_hold_minutes = 45
    enforce_hard_limits(cfg)
    return cfg


def _srow(path, mint="Abcpump", start=1_762_800_000.0):
    import json
    return {"start_ts": start, "end_ts": start + 3600, "mode": "paper", "mint": mint, "symbol": "T", "score": 0.7,
            "desk_label": "up", "sim_net_pct": 50.0, "cost_pct": 2.0, "p_up": None, "p_down": None,
            "p_neither": None, "path": json.dumps(path)}


def test_shadow_outcome_uses_current_rules():
    from desk.quant import parse_since
    cfg = _cfg()
    t = parse_since(cfg.quant.learn_since) + 60
    path = [[10, 1.0], [60, -7.0], [120, 40.0]]
    o = shadow_to_outcome(_srow(path, start=t), 0.5, cfg)
    assert o["label"] == "down" and o["ret_net_pct"] == pytest.approx(-9.0)   # current -4% stop, not the old +50%
    assert shadow_to_outcome(_srow(path, start=t - 7200), 0.5, cfg) is None    # before the rug protections
    assert shadow_to_outcome(_srow(path, mint="Abcxyz", start=t), 0.5, cfg) is None   # copycat mint
    frozen = [[i * 10, 2.0] for i in range(20)]
    assert shadow_to_outcome(_srow(frozen, start=t), 0.5, cfg) is None         # frozen DexScreener price


def test_old_trades_are_kept_but_not_learned_from():
    from desk.quant import parse_since, usable_trade
    cfg = _cfg()
    t = parse_since(cfg.quant.learn_since)
    assert not usable_trade({"ts": t - 10, "mint": "Apump"}, cfg)
    assert usable_trade({"ts": t + 10, "mint": "Apump"}, cfg)
    cfg.quant.learn_since = ""
    assert usable_trade({"ts": 5, "mint": "Apump"}, cfg)
