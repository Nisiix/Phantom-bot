"""Entry timing: the rule (desk/entry.py), the Executor waiting for the moment, the Controller testing rules."""
from __future__ import annotations

import asyncio
import json

from desk.agents.executor import Executor
from desk.agents.risk import Risk
from desk.brokers.paper import PaperBroker
from desk.config import Config, enforce_hard_limits
from desk.controller import build_spaces, parse_rows, simulate_exit, within_hard_limits
from desk.entry import EntryPlan, entry_path, entry_point, plan_from, rebase
from tests.test_rules import make_desk, signed_verdict, snap

NOW = {"mode": "now"}


def test_now_keeps_the_old_behaviour():
    path = [(20, -3.0), (40, 10.0)]
    assert entry_point(path, NOW) == -1
    assert entry_path(path, NOW) is path


def test_dip_buys_at_the_first_reading_under_the_level():
    path = [(20, -2.0), (40, -6.0), (60, -9.0), (80, 5.0)]
    v = {"mode": "dip", "window_minutes": 5, "dip_pct": 5}
    assert entry_point(path, v) == 1
    ep = entry_path(path, v)
    assert ep[0][0] == 20 and abs(ep[-1][1] - ((1.05 / 0.94) - 1) * 100) < 1e-9


def test_dip_skips_or_buys_at_window_end():
    path = [(60, 2.0), (200, 4.0), (400, 3.0), (700, 30.0)]
    assert entry_point(path, {"mode": "dip", "window_minutes": 5, "dip_pct": 5}) is None
    # fallback buy: the last reading inside the window (400 s > 300 s ends it -> reading at 200 s)
    assert entry_point(path, {"mode": "dip", "window_minutes": 5, "dip_pct": 5, "fallback": "buy"}) == 1


def test_bounce_never_buys_a_falling_knife():
    falling = [(20, -6.0), (40, -12.0), (60, -30.0), (80, -60.0)]
    v = {"mode": "bounce", "window_minutes": 5, "dip_pct": 5, "bounce_pct": 3}
    assert entry_point(falling, v) is None
    turning = [(20, -6.0), (40, -10.0), (60, -7.0)]     # +3.3% off the -10% low
    assert entry_point(turning, v) == 2


def test_confirm_checks_once():
    v = {"mode": "confirm", "window_minutes": 10, "confirm_minutes": 2, "confirm_pct": 0}
    assert entry_point([(60, -5.0), (130, 1.0)], v) == 1
    assert entry_point([(60, 5.0), (130, -1.0), (200, 40.0)], v) is None


def test_liquidity_leaving_cancels_the_plan():
    p = plan_from({"mode": "dip", "window_minutes": 10, "dip_pct": 5})
    assert p.update(30, -8.0, liq_ratio=0.5) == "cancel"
    assert "liquidità" in p.why


def test_rebase_moves_from_entry_price():
    out = rebase([(10, -10.0), (30, 8.0)], 0)
    assert out == [(20, ((1.08 / 0.9) - 1) * 100)]


def test_config_clamps_entry_settings():
    cfg = Config()
    cfg.entry.mode, cfg.entry.window_minutes, cfg.entry.fallback = "yolo", 60, "maybe"
    enforce_hard_limits(cfg)
    assert (cfg.entry.mode, cfg.entry.window_minutes, cfg.entry.fallback) == ("now", 10.0, "skip")
    assert within_hard_limits(Config(), ['[entry] mode = "dip"', "[entry] dip_pct = 3"])
    assert not within_hard_limits(Config(), ["[entry] window_minutes = 40"])


def _row(i, path, score=0.7):
    f = {"score": score, "age_min": 90, "liq_usd": 60_000}
    return {"start_ts": 1_800_000_000 + i * 60, "end_ts": 1_800_003_600 + i * 60, "mint": f"M{i}pump",
            "decision": "approve", "score": score, "sim_net_pct": 0.0, "cost_pct": 2.0,
            "features": json.dumps(f), "path": json.dumps(path)}


def test_controller_replays_entry_rule_and_offers_timing_space():
    cfg = Config()
    cfg.quant.learn_since = ""
    # every token spikes, dips 10% and then runs to +40%: buying the dip beats buying at approval
    path = [(20 * k, v) for k, v in enumerate([3, 1, -4, -10, -8, 0, 10, 20, 30, 40, 40, 40], start=1)]
    raw = [_row(i, path) for i in range(40)]
    rows = parse_rows(raw, 240, cfg)
    base = rows[0]["ret"]
    assert base == simulate_exit(rows[0]["path"], 2.0, cfg.risk.exit_stop_pct or cfg.risk.hard_stop_pct,
                                 cfg.risk.trail_activate_pct, cfg.risk.trail_distance_pct,
                                 float(cfg.risk.max_hold_minutes))
    spaces = {sp.agent: sp for sp in build_spaces(cfg, rows, [], {}, 30)}
    sp = spaces["Timing"]
    assert sp.current["mode"] == "now" and sp.returns(sp.current)[0] == base
    dip = {**sp.current, "mode": "dip", "dip_pct": 8.0, "window_minutes": 5.0}
    assert sp.returns(dip)[0] > base
    assert any(v["mode"] == "bounce" for v in sp.grid)
    assert sp.describe(dip) == ['[entry] mode = "dip"', "[entry] window_minutes = 5", "[entry] dip_pct = 8"]
    # with the dip rule live, every other agent measures from the dip entry
    cfg.entry.mode, cfg.entry.dip_pct, cfg.entry.window_minutes = "dip", 8.0, 5.0
    rows2 = parse_rows(raw, 240, cfg)
    assert rows2[0]["ret"] == sp.returns(dip)[0] and rows2[0]["epath"][0][0] == 20


class FakeDex:
    def __init__(self):
        self.price, self.liq = 0.001, 80_000

    async def snapshot(self, mint, fresh=True, lane=""):
        return snap(mint=mint, price_usd=self.price, price_native=self.price / 100, liquidity_usd=self.liq)

    async def snapshots(self, mints, fresh=False, lane=""):
        return {m: await self.snapshot(m) for m in mints}


class FakeJup:
    async def quote(self, i, o, amount, bps, lane=""):
        if i.startswith("So111"):
            return {"inAmount": str(amount), "outAmount": str(int(amount / 1e9 / 0.00001 * 1e6))}
        return {"outAmount": str(int(int(amount) * 0.00001 / 1e6 * 1e9 * 0.98))}


def test_executor_waits_for_the_dip_then_buys(tmp_path):
    desk = make_desk(tmp_path)
    desk.cfg.entry.mode, desk.cfg.entry.dip_pct = "dip", 5.0
    dex = FakeDex()
    desk.jup, desk.dex = FakeJup(), dex
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6

    async def go():
        await ex.handle_verdict(v)
        assert "M" not in desk.portfolio.positions and "M" in ex.plans      # waiting, not bought
        t0 = ex.plans["M"]["t0"]
        dex.price = 0.00098                                                  # -2%: not yet
        assert ex.timing_step(await dex.snapshots(["M"]), t0 + 30) == []
        dex.price = 0.00094                                                  # -6%: the moment
        ready = ex.timing_step(await dex.snapshots(["M"]), t0 + 60)
        assert ready == [v]
        await ex.handle_verdict(ready[0])
    asyncio.run(go())
    assert "M" in desk.portfolio.positions and not ex.plans
    assert "timing: ribasso" in desk.portfolio.positions["M"].entry_reason


def test_executor_drops_token_when_moment_never_comes(tmp_path):
    desk = make_desk(tmp_path)
    desk.cfg.entry.mode, desk.cfg.entry.window_minutes = "dip", 5.0
    dex = FakeDex()
    desk.jup, desk.dex = FakeJup(), dex
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    v = signed_verdict(desk)

    async def go():
        await ex.handle_verdict(v)
        t0 = ex.plans["M"]["t0"]
        ex.timing_step(await dex.snapshots(["M"]), t0 + 301)
        assert not ex.plans and "M" in ex.timing_done
        await ex.handle_verdict(signed_verdict(desk))                      # re-approved: not re-planned for 1 h
        assert not ex.plans
    asyncio.run(go())
    assert "M" not in desk.portfolio.positions


def test_executor_armed_plan_waits_for_fresh_verdict(tmp_path):
    desk = make_desk(tmp_path)
    desk.cfg.entry.mode = "dip"
    dex = FakeDex()
    desk.jup, desk.dex = FakeJup(), dex
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))

    async def go():
        await ex.handle_verdict(signed_verdict(desk))
        p = ex.plans["M"]
        p["verdict"] = signed_verdict(desk, age=500)                        # stale clearance
        dex.price = 0.0009
        assert ex.timing_step(await dex.snapshots(["M"]), p["t0"] + 40) == []
        assert p["plan"].state == "buy"                                     # armed
        fresh = signed_verdict(desk)
        fresh.clearance.report.rug["decimals"] = 6
        await ex.handle_verdict(fresh)
    asyncio.run(go())
    assert "M" in desk.portfolio.positions


def test_plan_now_buys_immediately():
    assert EntryPlan("now", 600).update(0, 0) == "buy"


def test_user_trial_is_measured_and_flagged_not_changed(tmp_path):
    from desk.controller import entry_trial, entry_trial_section
    from desk.controller_memory import load_overrides
    cfg = Config()
    cfg.quant.learn_since = ""
    cfg.entry.mode, cfg.entry.dip_pct, cfg.entry.window_minutes = "dip", 3.0, 5.0
    cfg.entry.trial_since = "2030-01-01T00:00:00Z"
    # every token dips 4% then dies: the dip rule buys and loses, buying at once loses less
    path = [(20 * k, v) for k, v in enumerate([5, -4, -20, -40, -60], start=1)]
    raw = [_row(i, path) for i in range(40)]
    rows = parse_rows(raw, 240, cfg)
    sp = {s.agent: s for s in build_spaces(cfg, rows, [], {}, 30)}["Timing"]
    assert entry_trial(cfg, sp)["status"] == "in prova"              # no token after the trial start yet
    cfg.entry.trial_since = "2020-01-01T00:00:00Z"
    t = entry_trial(cfg, sp)
    assert t["n"] == 40 and t["status"].startswith("da rimuovere")
    assert any("DA RIMUOVERE" in line for line in entry_trial_section(t))
    assert not within_hard_limits(cfg, ['[entry] mode = "now"'])     # the Controller never changes the trial
    cfg.data_dir = tmp_path
    (tmp_path / "controller_overrides_paper.json").write_text('{"entry.mode": "bounce"}')
    cfg.controller.auto_apply = "always"
    load_overrides(cfg)
    assert cfg.entry.mode == "dip"


def test_price_from_another_pool_is_not_a_dip(tmp_path):
    """BERNIE 2026-11-11: reference from a dust SOL pool, next tick from the real pool = fake -51% "dip"."""
    desk = make_desk(tmp_path)
    desk.cfg.entry.mode, desk.cfg.entry.dip_pct = "dip", 3.0
    dex = FakeDex()
    desk.jup, desk.dex = FakeJup(), dex
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))

    async def go():
        await ex.handle_verdict(signed_verdict(desk))
        p = ex.plans["M"]
        other = {"M": snap(pair_address="OTHER", price_usd=0.0005)}       # -50%, but read on another pool
        assert ex.timing_step(other, p["t0"] + 30) == []
        assert p["plan"].state != "buy"
    asyncio.run(go())
    assert "M" not in desk.portfolio.positions
