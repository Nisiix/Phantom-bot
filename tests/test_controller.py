"""Controller (Controllore): behaviour checks, offline experiments, proposals within the hard limits."""
from __future__ import annotations

import asyncio
import json
import random
import time

from desk.agents.analyst import score_token
from desk.agents.controller import Controller
from desk.config import Config
from desk.controller import (analyst_score, build_report, health_checks, proposals, run_experiments,
                             simulate_exit, within_hard_limits)
from desk.shadow import Shadow, ShadowTracker
from tests.test_rules import make_desk, position, snap


def _snapshot(t, **over):
    s = {"t": t, "cfg": {"max_open": 4, "max_pos_pct": 8.0, "hard_stop": -6.0, "trail_activate": 12.0,
                         "min_score": 0.6},
         "agents": {n: {"status": "working", "heartbeat": t, "errors": 0, "last": "", "counters": {}}
                    for n in ("Scout", "Filter", "Analyst", "Executor", "Risk", "Logger", "Controller")},
         "queues": {"candidates": 0, "passes": 0, "verdicts": 0, "exits": 0, "log": 0},
         "positions": [], "equity": 10.0, "trades": [], "http": {},
         "shadow": {"enabled": True, "oldest_age": 100, "horizon": 3600},
         "funnel": {"filter_seen": 0, "filter_pass": 0, "filter_reasons": [], "analyst_seen": 0, "analyst_pass": 0,
                    "analyst_avg": 0, "analyst_max": 0, "exec_rejects": 0, "exec_buys": 0, "exec_reasons": []}}
    s.update(over)
    return s


def test_health_all_good_and_rule_breaches():
    t = time.time()
    ok = health_checks(_snapshot(t))
    assert [f["level"] for f in ok] == ["ok"]
    bad = _snapshot(t, positions=[
        {"symbol": "BIG", "cost": 2.0, "pnl_pct": -9.0, "peak_pct": 20.0, "trailing": False, "exiting": False,
         "price_age": 5, "size_pct": 10.0}],
        trades=[{"symbol": "LOW", "score": 0.5, "pnl_pct": -15.0, "exit_reason": "HARD_STOP: x"}])
    checks = {f["check"]: f["level"] for f in health_checks(bad)}
    assert checks["posizione oltre l'8%"] == "critical"
    assert checks["posizione sotto lo stop"] == "critical"
    assert checks["trailing non armato"] == "warn"
    assert checks["acquisto sotto soglia"] == "critical"
    assert checks["stop eseguito molto peggio"] == "warn"


def test_health_progress_and_errors():
    t = time.time()
    old = _snapshot(t - 600)
    old["agents"]["Scout"]["counters"] = {"discovered": 10, "sent": 5}
    now = _snapshot(t)
    now["agents"]["Scout"]["counters"] = {"discovered": 10, "sent": 9}
    now["agents"]["Filter"]["errors"] = 25
    now["agents"]["Risk"]["heartbeat"] = t - 90
    now["positions"] = [{"symbol": "A", "cost": 0.5, "pnl_pct": 1, "peak_pct": 1, "trailing": False,
                         "exiting": False, "price_age": 1}]
    checks = {f["check"]: f for f in health_checks(now, old)}
    assert "nessun token scoperto" in checks
    assert checks["Filter fermo"]["level"] == "critical"
    assert checks["errori ripetuti"]["agent"] == "Filter"
    assert checks["Risk non sorveglia le posizioni"]["level"] == "critical"


def test_simulate_exit_rules():
    path = [(20, 5.0), (40, 20.0), (60, 30.0), (80, 22.0), (100, 10.0)]
    # cost 2: net 3, 18, 28, 20 -> trailing 6% from 28 = value 1.28*0.94 = 1.2032 -> exits at net 20
    assert abs(simulate_exit(path, 2.0, -6, 12, 6) - 20.0) < 1e-9
    assert simulate_exit([(20, -10.0), (40, 50.0)], 2.0, -6, 12, 6) == -12.0
    assert simulate_exit([(20, 5.0), (900, 8.0), (1900, 30.0)], 2.0, -6, 12, 6, max_hold_min=15) == 6.0


def test_analyst_rescore_matches_live_score():
    info: dict = {}
    m = snap()
    rug = {"top10_pct": 12.0, "holders_total": 800, "insider_pct": 3, "risks": []}
    score, breakdown, _ = score_token(m, rug, {}, None, info)
    f = {**{f"c_{k}": v for k, v in breakdown.items()}, **info}
    from desk.controller import BASE_WEIGHTS
    assert abs(analyst_score(f, BASE_WEIGHTS) - score) < 0.01
    assert analyst_score({"c_liquidity": 1.0}, BASE_WEIGHTS) is None     # old rows without raw_score


def _rows(n, seed=1):
    """Synthetic shadow rows: high 'c_buy_pressure' tokens win, low ones lose."""
    rng = random.Random(seed)
    an, fi = [], []
    t0 = time.time() - n * 60
    for i in range(n):
        bp = rng.random()
        win = bp > 0.6
        path = [(20 * k, (k * 1.5 if win else -k * 0.8)) for k in range(1, 40)]
        f = {"c_liquidity": 0.6, "c_turnover": 0.6, "c_buy_pressure": bp, "c_momentum": 0.6, "c_holders": 0.6,
             "c_hygiene": 1.0, "c_website": 0.5, "c_maturity": 0.8, "raw_score": 0.6, "cap_score": None,
             "has_site": True, "liq_usd": 30_000 + 1000 * i, "age_min": 30 + i, "source": "pumpfun:new",
             "top10_pct": 15.0, "vol_h1": 80_000, "mkt_meme_h1": rng.choice((-3.0, 0.0, 3.0))}
        f["score"] = analyst_score(f, __import__("desk.controller", fromlist=["BASE_WEIGHTS"]).BASE_WEIGHTS)
        ret = 20.0 if win else -8.0
        an.append({"start_ts": t0 + i * 60, "mint": f"M{i}", "score": f["score"], "decision": "approve",
                   "sim_net_pct": ret, "cost_pct": 2.0, "features": json.dumps(f), "path": json.dumps(path)})
        fi.append({**an[-1], "decision": "approve"})
    return an, fi


def test_experiments_find_better_weights_and_respect_hard_limits():
    cfg = Config()
    an, fi = _rows(300)
    exp = run_experiments(cfg, an, fi, {}, {}, seed=7)
    assert exp["tried"] > 50 and exp["analyst_rows"] == 300 and exp["with_path"] == 300
    props = proposals(exp, cfg)
    assert any(p["agent"] == "Analyst" and "buy_pressure" in " ".join(p["changes"]) for p in props)
    for p in props:
        assert within_hard_limits(cfg, p["changes"])
    assert not within_hard_limits(cfg, ["[analyst] min_score = 0.5"])
    assert not within_hard_limits(cfg, ["[risk] hard_stop_pct = -8"])
    assert within_hard_limits(cfg, ["[risk] hard_stop_pct = -4"])
    text = build_report(health_checks(_snapshot(time.time())), exp, cfg, exp["tried"], 1)
    assert "## Proposte" in text and "Analyst" in text
    # the winners are remembered and retried next round
    assert exp["best"]["Analyst"]


def test_experiments_with_little_data_say_so():
    cfg = Config()
    an, fi = _rows(10)
    exp = run_experiments(cfg, an, fi, {}, {}, seed=1)
    assert proposals(exp, cfg) == []
    assert all("dati insufficienti" in r["verdict"] for r in exp["results"])


def test_shadow_records_price_path(tmp_path):
    desk = make_desk(tmp_path)
    st = ShadowTracker(desk, lambda *a, **k: None)
    s = Shadow("M:analyst", "M", "TST", "analyst", "approve", "x", 0.7, 1.0, 50_000, 2.0, time.time() - 3600)
    s.path = [(20, 1.5), (40, -2.0)]
    s.desk_label = s.model_label = "neither"
    s.sim_ret = -4.0
    st._store(s)
    row = desk.store.q("SELECT path FROM shadow")[0]
    assert json.loads(row["path"]) == [[20, 1.5], [40, -2.0]]


def test_controller_agent_round_is_read_only(tmp_path):
    desk = make_desk(tmp_path)
    desk.portfolio.open(position())
    an, _ = _rows(120)
    for r in an:
        desk.store.add_shadow({"start_ts": r["start_ts"], "end_ts": r["start_ts"] + 3600, "mode": "paper",
                               "mint": r["mint"], "symbol": "T", "stage": "analyst", "decision": "approve",
                               "reason": "", "score": r["score"], "traded": 0, "ref_price": 1.0, "ref_liq": 1.0,
                               "cost_pct": 2.0, "desk_label": "up", "sim_net_pct": r["sim_net_pct"],
                               "features": r["features"], "path": r["path"]})
    before = json.dumps(desk.cfg.analyst.__dict__, sort_keys=True)
    ctl = Controller(desk)
    ctl.health = health_checks(ctl.collect())
    exp = asyncio.run(asyncio.to_thread(ctl._round))
    ctl.exp = exp
    text = ctl.write_report()
    assert exp["analyst_rows"] == 120 and "Controllore" in text
    assert (tmp_path / "reports" / "controller_paper_latest.md").exists()
    assert json.dumps(desk.cfg.analyst.__dict__, sort_keys=True) == before      # config never touched
    assert ctl.summary()["health"]


def test_entry_size_from_reason():
    from desk.agents.controller import entry_size_pct
    assert abs(entry_size_pct("score 0.7 | size 0.8000 SOL = 8.0% of 10.000 | impact 1%") - 8.0) < 1e-9
    assert entry_size_pct("test") is None


def test_wider_stops_are_measured_but_never_proposed():
    """Volatile young tokens that dip -8% then run: a wider stop wins, but it is shown as a measure only."""
    from desk.controller import WIDE_STOPS
    an, fi = _rows(300, seed=3)
    for r in an:                                   # every path dips to -8% first, winners then run to +40%
        f = json.loads(r["features"])
        win = f["c_buy_pressure"] > 0.6
        path = [(20, -4.0), (40, -8.0), (60, -5.0)] + [(80 + 20 * k, (k * 3.0 if win else -12.0 - k)) for k in range(14)]
        r["path"] = json.dumps(path)
    cfg = Config()
    exp = run_experiments(cfg, an, fi, {}, {}, seed=5)
    wide = [r for r in exp["results"] if r["agent"] == WIDE_STOPS and "variant" in r]
    assert wide and any(r["variant"]["hard_stop_pct"] < -6 and r["verdict"] in ("solida", "promettente") for r in wide)
    assert not any(p["agent"] == WIDE_STOPS for p in proposals(exp, cfg))
    for p in proposals(exp, cfg):
        assert within_hard_limits(cfg, p["changes"])
    text = build_report(health_checks(_snapshot(time.time())), exp, cfg, exp["tried"], 1)
    assert "Stop più larghi del -6% (solo misura" in text and "hard_stop_pct = -" in text
