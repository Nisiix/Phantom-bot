"""Controller (Controllore): behaviour checks, offline experiments, proposals within the hard limits."""
from __future__ import annotations

import asyncio
import json
import random
import time

from desk.agents.analyst import score_token
from desk.agents.controller import Controller
from desk.config import Config
from desk.controller import (analyst_score, build_report, health_checks, parse_rows, path_problem, proposals,
                             run_experiments,
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
        an.append({"start_ts": t0 + i * 60, "mint": f"M{i}pump", "score": f["score"], "decision": "approve",
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


def test_reset_paper(tmp_path):
    from run import reset_paper
    from desk.portfolio import Portfolio
    from desk.store import Store
    cfg = Config()
    cfg.data_dir = tmp_path
    desk = make_desk(tmp_path)
    desk.portfolio.open(position())
    desk.portfolio.set_cash(3.0)
    desk.store.add_equity(time.time(), 3.0, 4.0, 150.0)
    desk.store.add_shadow({"start_ts": 1.0, "end_ts": 2.0, "mode": "paper", "mint": "X", "stage": "analyst"})
    msg = reset_paper(cfg)
    assert "reset to 10" in msg and list(tmp_path.glob("paper_backup_*.db"))
    st = Store(tmp_path / "paper.db")
    pf = Portfolio(st, "paper", 10.0)
    assert pf.cash_sol == 10.0 and pf.starting_cash == 10.0 and not pf.positions and pf.total_pnl_sol == 0
    assert st.q("SELECT COUNT(*) AS n FROM equity")[0]["n"] == 0
    assert st.q("SELECT COUNT(*) AS n FROM shadow")[0]["n"] == 1      # learning data kept


def test_reset_keeps_live_gate_record(tmp_path):
    from run import reset_paper
    from desk.recap import live_gate_status
    from desk.store import Store
    cfg = Config()
    cfg.data_dir = tmp_path
    st = Store(tmp_path / "paper.db")
    for i in range(20):
        st.add_trade({"mint": f"M{i}", "symbol": "T", "mode": "paper", "entry_ts": i, "exit_ts": i + 1,
                      "cost_sol": 0.5, "proceeds_sol": 0.55, "pnl_sol": 0.05, "pnl_pct": 10.0,
                      "peak_pnl_pct": 12.0, "score": 0.7, "entry_reason": "", "exit_reason": ""})
    st.add_equity(1.0, 10.0, 12.0, 150.0)
    reset_paper(cfg)
    st = Store(tmp_path / "paper.db")
    st.add_trade({"mint": "N", "symbol": "T", "mode": "paper", "entry_ts": 50, "exit_ts": 51, "cost_sol": 0.5,
                  "proceeds_sol": 0.6, "pnl_sol": 0.1, "pnl_pct": 20.0, "peak_pnl_pct": 22.0, "score": 0.7,
                  "entry_reason": "", "exit_reason": ""})
    st.add_equity(60.0, 10.0, 10.1, 150.0)
    ok, msgs = live_gate_status(st, cfg.live_gate)
    assert "paper trades 21/30" in msgs[0]          # pre-reset trades still count, 12 -> 10 jump is no drawdown
    assert not any("drawdown" in m for m in msgs)
    assert len(st.all_trades()) == 1                # but the account's own stats start from zero


def test_outlier_capped_and_weights_configurable(tmp_path):
    assert simulate_exit([(20, 900000.0)], 2.0, -6, 12, 6) == 300.0
    p = tmp_path / "c.toml"
    p.write_text('[analyst]\nweights = { holders = 0.30, buy_pressure = 0.05 }\n', encoding="utf-8")
    from desk.config import load_config
    cfg = load_config(p)
    assert cfg.analyst.weights == {"holders": 0.30, "buy_pressure": 0.05}
    m, rug = snap(), {"top10_pct": 6.0, "holders_total": 4000, "insider_pct": 0, "risks": []}
    base, _, _ = score_token(m, rug, {}, None)
    tuned, _, _ = score_token(m, rug, {}, cfg.analyst)
    assert tuned != base
    p.write_text('[analyst]\nweights = { nonsense = 1 }\n', encoding="utf-8")
    bad = load_config(p)
    assert bad.analyst.weights == {} and any("weights" in w for w in bad.warnings)
    assert within_hard_limits(cfg, ["[analyst] weights = { holders = 0.30 }", "[analyst] min_score = 0.65"])


def test_reset_never_drops_learning_memory(tmp_path):
    from run import reset_paper
    from desk.agents.risk import Risk
    cfg = Config()
    cfg.data_dir = tmp_path
    desk = make_desk(tmp_path)
    desk.store.add_trade({"mint": "LOSER", "symbol": "T", "mode": "paper", "entry_ts": 1, "exit_ts": 2,
                          "cost_sol": 0.5, "proceeds_sol": 0.4, "pnl_sol": -0.1, "pnl_pct": -20.0,
                          "peak_pnl_pct": 0.0, "score": 0.7, "entry_reason": "", "exit_reason": ""})
    desk.store.kv_set("risk_state", {"history": {"OTHER": {"ts": 1, "pnl_sol": -0.2}}, "consecutive_losses": 2})
    reset_paper(cfg)
    desk2 = make_desk(tmp_path)
    risk = Risk(desk2)
    assert risk.consecutive_losses == 0
    assert not risk.approve_entry("LOSER")[0] and not risk.approve_entry("OTHER")[0]


def _fill_shadow(desk, rows):
    for r in rows:
        desk.store.add_shadow({"start_ts": r["start_ts"], "end_ts": r["start_ts"] + 3600, "mode": "paper",
                               "mint": r["mint"], "symbol": "T", "stage": "analyst", "decision": "approve",
                               "reason": "", "score": r["score"], "traded": 0, "ref_price": 1.0, "ref_liq": 1.0,
                               "cost_pct": 2.0, "desk_label": "up", "sim_net_pct": r["sim_net_pct"],
                               "features": r["features"], "path": r["path"]})


def test_controller_never_applies_only_proposes(tmp_path):
    """Base rule (user, 2026-11-11): the Controller only proposes, the user decides."""
    from desk import controller_memory as mem
    from desk.config import load_config
    desk = make_desk(tmp_path)
    desk.cfg.controller.confirm_rounds = 1
    an, _ = _rows(300)
    _fill_shadow(desk, an)
    ctl = Controller(desk)
    ctl.health = []
    before = (desk.cfg.analyst.min_score, dict(desk.cfg.analyst.weights or {}), desk.cfg.risk.trail_activate_pct)
    exp = ctl._round(mem.pending_reviews(desk.store))
    props = proposals(exp, desk.cfg)
    assert props                                         # it does find winners...
    assert ctl.auto_apply(exp, props) == 0               # ...but never applies them
    assert mem.decisions(desk.store) == []
    assert (desk.cfg.analyst.min_score, dict(desk.cfg.analyst.weights or {}),
            desk.cfg.risk.trail_activate_pct) == before
    assert not mem.overrides_path(tmp_path, "paper").exists()
    ctl.exp = exp
    s = ctl.summary()
    assert s["proposals"] and not s["auto_on"]
    assert "non le applica" in ctl.write_report()
    # config.toml cannot switch it back on
    toml = tmp_path / "c.toml"
    toml.write_text(f'data_dir = "{tmp_path.as_posix()}"\n[controller]\nauto_apply = "always"\n', encoding="utf-8")
    cfg2 = load_config(toml)
    assert cfg2.controller.auto_apply == "off" and any("only proposes" in w for w in cfg2.warnings)
    # values it applied before the rule stay in use until the user decides (loaded whatever auto_apply says)
    mem.overrides_path(tmp_path, "paper").write_text('{"risk.trail_activate_pct": 30.0}', encoding="utf-8")
    cfg3 = load_config(toml)
    assert cfg3.controller_overrides == {"risk.trail_activate_pct": 30.0} and cfg3.risk.trail_activate_pct == 30.0
    # memory and report survive a restart
    ctl._save_state()
    ctl2 = Controller(desk)
    assert ctl2.report and "Decisioni del Controllore" in ctl2.report and ctl2.exp and ctl2.rounds == ctl.rounds


def test_disproved_decision_becomes_a_rollback_proposal(tmp_path):
    from desk import controller_memory as mem
    desk = make_desk(tmp_path)
    ctl = Controller(desk)
    exp = {"tried": 1}
    before, after = mem.set_values(desk.cfg, [("risk", "max_hold_minutes", 45)])
    p = {"agent": "Risk", "changes": ["[risk] max_hold_minutes = 45"], "verdict": "solida", "gain": 1.0, "lo": 0.1,
         "hi": 2.0, "n": 50, "valid_n": 15, "also": []}
    did = mem.record(desk.store, desk.cfg, "apply", "Risk", p["changes"], before, after, p, 1, exp, "test",
                     {"x": 0}, {"x": 1})
    ctl.review({"reviews": [{"id": did, "n": 40, "mean": -5.0, "lo": -8.0, "hi": -2.0}]})
    assert desk.cfg.risk.max_hold_minutes == 45                 # NOT rolled back by the Controller
    ds = mem.decisions(desk.store)
    assert [d["kind"] for d in ds] == ["apply"] and ds[0]["status"] == "smentita"
    rb = [x for x in ctl.all_proposals() if x.get("kind") == "rollback"]
    assert rb and rb[0]["changes"] == [f"[risk] max_hold_minutes = {before['risk.max_hold_minutes']:g}"]
    assert rb[0]["gain"] == 5.0 and rb[0]["decision"] == did
    # once the user changed the value, there is nothing left to propose
    desk.cfg.risk.max_hold_minutes = 30
    assert not [x for x in ctl.all_proposals() if x.get("kind") == "rollback"]
    assert next(d for d in ds if d["id"] == did)["status"] == "smentita"
    refuted = mem.refuted_keys(desk.store)
    picked, _ = mem.choose([{**p, "situation": "all"}], desk.cfg, {}, {}, refuted, time.time())
    assert picked == []


def test_choose_needs_confirmation_and_skips_non_config(tmp_path):
    from desk import controller_memory as mem
    cfg = Config()
    p = {"agent": "Risk", "situation": "all", "changes": ["[risk] max_hold_minutes = 30"], "verdict": "promettente"}
    hint = {"agent": "Executor", "situation": "all", "changes": ["accettare i token rifiutati per 'x'"],
            "verdict": "solida"}
    picked, st = mem.choose([p, hint], cfg, {}, {}, set(), time.time())
    assert picked == [] and list(st.values()) == [1]
    picked, st = mem.choose([p], cfg, st, {}, set(), time.time())
    picked, st = mem.choose([p], cfg, st, {}, set(), time.time())
    assert len(picked) == 1                                         # third round in a row
    off = {"agent": "Scout", "situation": "all", "verdict": "solida",
           "changes": ["[scout] pumpportal = false", "[scout] geckoterminal = false", "[scout] dexscreener = false"]}
    assert mem.choose([off], cfg, {}, {}, set(), time.time())[0] == []


def test_shadow_followups_survive_restart(tmp_path):
    desk = make_desk(tmp_path)
    st = ShadowTracker(desk, lambda *a, **k: None)
    st.start("A", "AAA", "analyst", "approve", "x", 1.0, 50_000, score=0.7, features={"liq_usd": 50_000})
    st.active["A:analyst"].path = [(20, 1.0)]
    old = Shadow("B:analyst", "B", "BBB", "analyst", "approve", "x", 0.7, 1.0, 50_000, 2.0, time.time() - 7200)
    st.active[old.key] = old
    st.save()
    st2 = ShadowTracker(desk, lambda *a, **k: None)        # restart
    assert "A:analyst" in st2.active and st2.active["A:analyst"].path == [(20, 1.0)]
    assert "B:analyst" not in st2.active                    # its 60 minutes ran out: stored, not lost
    assert desk.store.q("SELECT mint FROM shadow")[0]["mint"] == "B"


def test_untrustworthy_paths_and_copycats_are_left_out_and_exits_are_replayed():
    cfg = Config()
    cfg.scout.venues = "pumpfun"
    cfg.risk.hard_stop_pct, cfg.risk.trail_activate_pct, cfg.risk.trail_distance_pct = -4.0, 25.0, 8.0
    cfg.risk.max_hold_minutes = 45
    flat = [(20 * k, 2.65) for k in range(1, 60)]                      # pool emptied: DexScreener price frozen
    assert path_problem(flat) == "frozen"
    assert path_problem([(7, 3.2), (28, 1376622.0), (60, 22.9)]) == "glitch"
    assert path_problem([(20, 1.0), (40, 2.0), (60, 1.5)]) == ""
    # +155% at minute 44, then a rug to -99% at minute 50: the 45-min time exit takes the +155%
    runner = [(60 * m, 3.5 * m) for m in range(1, 45)] + [(2700, 155.0), (3000, -99.0), (3600, -99.0)]

    def row(mint, path, sim):
        return {"start_ts": 1.0, "mint": mint, "score": 0.7, "decision": "approve", "sim_net_pct": sim,
                "cost_pct": 2.0, "features": json.dumps({"score": 0.7}), "path": json.dumps(path)}
    skipped: dict = {}
    rows = parse_rows([row("AAApump", runner, -101.0), row("BBB", runner, 50.0), row("CCCpump", flat, 0.6)],
                      60, cfg, skipped)
    assert [x["mint"] for x in rows] == ["AAApump"]
    assert abs(rows[0]["ret"] - 153.0) < 1e-9                          # replayed, not the live -101
    assert skipped == {"copycat": 1, "frozen": 1}
    assert parse_rows([row("BBB", runner, 50.0)], 60)[0]["ret"] == 50.0  # without cfg: old behaviour


def test_shadow_simulation_takes_the_time_exit():
    from desk.shadow import update_shadow
    s = Shadow(key="M", mint="M", symbol="T", stage="analyst", decision="approve", reason="", score=0.7,
               ref_price=1.0, ref_liq=1.0, cost_pct=2.0, start=0.0)
    update_shadow(s, 2.5, 2700, -4, 25, 8, 45)       # +148% net at the 45-min mark: sold there
    update_shadow(s, 0.01, 3000, -4, 25, 8, 45)
    assert not s.sim_open and abs(s.sim_ret - 148.0) < 1e-9


def test_paper_stop_trial_is_paper_only_and_not_moved_by_the_controller():
    from desk.agents.risk import exit_decision
    from desk.config import enforce_hard_limits
    cfg = Config()
    cfg.risk.hard_stop_pct, cfg.risk.paper_hard_stop_pct = -4.0, -25.0
    enforce_hard_limits(cfg)
    assert cfg.risk.exit_stop_pct == -25.0 and cfg.risk.hard_stop_pct == -4.0 and not cfg.warnings
    assert exit_decision(position(value=0.9), cfg.risk, 0, None)[0] is None     # -10% holds in the paper trial
    assert exit_decision(position(value=0.74), cfg.risk, 0, None)[0] == "HARD_STOP"
    assert not within_hard_limits(cfg, ["[risk] hard_stop_pct = -5"])  # the Controller leaves the trial alone
    assert within_hard_limits(cfg, ["[risk] trail_activate_pct = 30"])
    live = Config()
    live.mode = "live"
    live.risk.hard_stop_pct, live.risk.paper_hard_stop_pct = -4.0, -25.0
    enforce_hard_limits(live)
    assert live.risk.exit_stop_pct == -4.0                            # never in live
    bad = Config()
    bad.risk.paper_hard_stop_pct = -60.0
    enforce_hard_limits(bad)
    assert bad.risk.paper_hard_stop_pct == 0.0 and bad.risk.exit_stop_pct == bad.risk.hard_stop_pct


def test_saved_winners_from_before_a_new_setting_still_compete():
    """A winner stored before a setting existed (profit_lock_fee_multiple, 2026-11-11) lacks its key: it takes
    the current value and keeps competing; the round must not crash when it wins (KeyError seen at restart)."""
    import random as _random
    from desk.controller import Space, evaluate_space
    rows = [{"ts": i, "sit": ["all"]} for i in range(60)]
    cur = {"a": 1.0, "new_key": 2.0}
    sp = Space("Risk", rows, cur, lambda v: [v["a"] * v["new_key"]] * len(rows), [], lambda rng: cur,
               lambda v: [f"{k} = {v[k]:g}" for k in cur if abs(v[k] - cur[k]) > 1e-9], "note")
    res, tried = evaluate_space(sp, _random.Random(1), 0, [{"a": 5.0}], 0.3, 30)
    best = next(r for r in res if r["situation"] == "all")
    assert tried == 2 and best["variant"] == {"a": 5.0, "new_key": 2.0} and best["changes"] == ["a = 5"]
