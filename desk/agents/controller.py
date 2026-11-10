"""CONTROLLER (Controllore) agent — watches the other agents and tests other settings offline.

Read-only by design: it never edits the configuration, never places an order and makes no API calls
(everything comes from memory and the local database). See desk/controller.py for the logic.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections import Counter, deque

from ..controller import build_report, health_checks, proposals, reason_category, run_experiments
from .base import Agent


SIZE_RE = re.compile(r"size ([\d.]+) SOL = [\d.]+% of ([\d.]+)")


def entry_size_pct(reason: str) -> float | None:
    """Position size as % of the cash at entry, read from the Executor's entry reason."""
    m = SIZE_RE.search(reason or "")
    if not m or float(m.group(2)) <= 0:
        return None
    return float(m.group(1)) / float(m.group(2)) * 100


class Controller(Agent):
    name = "Controller"

    def __init__(self, desk, risk=None) -> None:
        super().__init__(desk)
        self.c = self.cfg.controller
        self.risk = risk
        self.snaps: deque = deque(maxlen=200)
        self.health: list[dict] = []
        self.exp: dict | None = None
        self.report: str = ""
        self.report_ts: float = 0.0
        self.next_round: float = 0.0
        st = desk.store.kv_get("controller_state", {}) or {}
        self.rounds: int = st.get("rounds", 0)
        self.trials: int = st.get("trials", 0)
        self.best: dict = st.get("best", {})
        self._announced: set[str] = set()
        desk.controller = self

    async def run(self) -> None:
        if not self.c.enabled:
            self.status("idle")
            self.emit("info", "Controller disabled ([controller] enabled = false)")
            return
        self.status("working")
        self.emit("info", f"Controller online: checks the agents every {self.c.health_seconds:g}s, tests other "
                          f"settings on logged data every {self.c.interval_minutes:g} min (proposals only, "
                          "nothing is changed automatically, no API calls)")
        self.next_round = time.time() + 60          # first round after a minute (let the desk start up)
        await asyncio.gather(self.supervised("health", self.health_loop),
                             self.supervised("experiments", self.experiment_loop))

    # ── behaviour checks ──
    def collect(self) -> dict:
        d, t = self.desk, time.time()
        pf = d.portfolio
        exiting = set(getattr(self.risk, "exiting", {}) or {})
        cfg = self.cfg
        shadow = d.shadow
        oldest = max((t - s.start for s in shadow.active.values()), default=0.0) if shadow is not None else 0.0
        return {
            "t": t,
            "cfg": {"max_open": cfg.executor.max_open_positions, "max_pos_pct": cfg.executor.max_position_pct,
                    "hard_stop": cfg.risk.hard_stop_pct, "trail_activate": cfg.risk.trail_activate_pct,
                    "min_score": cfg.analyst.min_score},
            "agents": {n: {"status": s.status, "heartbeat": s.heartbeat, "errors": s.errors, "last": s.last,
                           "counters": dict(s.counters)} for n, s in d.hub.agents.items()},
            "queues": {"candidates": d.candidates.qsize(), "passes": d.passes.qsize(),
                       "verdicts": d.verdicts.qsize(), "exits": d.exits.qsize(), "log": d.hub.log_queue.qsize()},
            "positions": [{"symbol": p.symbol, "cost": p.cost_sol, "size_pct": entry_size_pct(p.entry_reason), "pnl_pct": p.pnl_pct, "peak_pct": p.peak_pnl_pct,
                           "trailing": p.trailing_active, "exiting": p.mint in exiting or p.mint in d.in_flight,
                           "price_age": t - p.last_price_ts if p.last_price_ts else 0.0}
                          for p in pf.positions.values()],
            "equity": pf.equity_sol,
            "trades": d.store.q("SELECT symbol,score,pnl_pct,exit_reason FROM trades WHERE exit_ts>=?",
                                (t - 600,)),
            "shadow": {"enabled": cfg.quant.shadow_tracking and shadow is not None, "oldest_age": oldest,
                       "horizon": cfg.quant.horizon_minutes * 60},
            "http": {h: dict(v) for h, v in getattr(d.http, "stats", {}).items()},
            "funnel": self.funnel(t),
        }

    def funnel(self, t: float) -> dict:
        rows = self.desk.store.q("SELECT stage,outcome,reason FROM decisions WHERE ts>=?", (t - 3600,))
        fr = [r for r in rows if r["stage"] == "filter"]
        an = [r for r in rows if r["stage"] == "analyst"]
        ex = [r for r in rows if r["stage"] == "executor"]
        scores = []
        for r in an:
            try:
                scores.append(float(r["reason"].split()[1]))
            except (IndexError, ValueError, AttributeError):
                pass
        rej = [r for r in ex if r["outcome"] in ("reject", "refuse")]
        return {
            "filter_seen": len(fr), "filter_pass": sum(r["outcome"] == "pass" for r in fr),
            "filter_reasons": Counter(reason_category(r["reason"]) for r in fr
                                      if r["outcome"] != "pass").most_common(5),
            "analyst_seen": len(an), "analyst_pass": sum(r["outcome"] == "approve" for r in an),
            "analyst_avg": sum(scores) / len(scores) if scores else 0.0, "analyst_max": max(scores, default=0.0),
            "exec_rejects": len(rej), "exec_buys": sum(r["outcome"] == "buy" for r in ex),
            "exec_reasons": Counter(reason_category(r["reason"]) for r in rej).most_common(5),
        }

    async def health_loop(self) -> None:
        while not self.desk.stop.is_set():
            s = self.collect()
            self.snaps.append(s)
            old = next((x for x in self.snaps if s["t"] - x["t"] <= 600), None)
            old = old if old is not s else None
            self.health = health_checks(s, old)
            bad = [f for f in self.health if f["level"] in ("warn", "critical")]
            self.state.counters["checks"] = self.state.counters.get("checks", 0) + 1
            self.state.counters["alerts"] = len(bad)
            for f in bad:
                key = f"{f['agent']}|{f['check']}"
                if key not in self._announced:      # announce each problem once, again only after it cleared
                    self.emit("warn" if f["level"] == "warn" else "error",
                              f"{f['agent']}: {f['check']} — {f['detail']}", check=f["check"], level=f["level"])
            self._announced = {f"{f['agent']}|{f['check']}" for f in bad}
            if not await self.sleep(self.c.health_seconds):
                return

    # ── experiments ──
    def _load(self) -> tuple[list[dict], list[dict], dict[str, list[str]]]:
        """Runs in a worker thread with its own read-only connection (the desk keeps writing meanwhile)."""
        since = time.time() - self.c.lookback_days * 86400
        db = sqlite3.connect(f"file:{self.desk.store.path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            cols = "start_ts,mint,score,decision,sim_net_pct,cost_pct,features,path"
            an = [dict(r) for r in db.execute(
                f"SELECT {cols} FROM shadow WHERE stage='analyst' AND features IS NOT NULL AND start_ts>=? "
                "ORDER BY start_ts DESC LIMIT 6000", (since,))]
            fi = [dict(r) for r in db.execute(
                f"SELECT {cols} FROM shadow WHERE stage='filter' AND features IS NOT NULL AND start_ts>=? "
                "ORDER BY start_ts DESC LIMIT 20000", (since,))]
            rej: dict[str, list[str]] = {}
            for r in db.execute("SELECT mint,reason FROM decisions WHERE stage='executor' AND outcome IN "
                                "('reject','refuse') AND ts>=?", (since,)):
                rej.setdefault(r["mint"], []).append(reason_category(r["reason"]))
        finally:
            db.close()
        return an, fi, rej

    def _round(self) -> dict:
        an, fi, rej = self._load()
        return run_experiments(self.cfg, an, fi, rej, self.best)

    async def experiment_loop(self) -> None:
        while not self.desk.stop.is_set():
            wait = self.next_round - time.time()
            if wait > 0:
                if not await self.sleep(min(wait, 30)):
                    return
                continue
            self.status("working")
            t0 = time.time()
            exp = await asyncio.to_thread(self._round)
            self.exp = exp
            self.rounds += 1
            self.trials += exp["tried"]
            self.best = exp["best"]
            self.desk.store.kv_set("controller_state", {"rounds": self.rounds, "trials": self.trials,
                                                        "best": self.best})
            self.write_report()
            props = proposals(exp, self.cfg)
            self.desk.store.db.execute(
                "INSERT INTO controller_runs(ts,trials,health,proposals,report) VALUES(?,?,?,?,?)",
                (time.time(), exp["tried"], json.dumps(self.health), json.dumps(props, default=str), self.report))
            self.state.counters["rounds"] = self.rounds
            self.state.counters["trials"] = self.trials
            self.state.counters["proposals"] = len(props)
            solid = sum(p["verdict"] == "solida" for p in props)
            self.emit("info", f"test round {self.rounds}: {exp['tried']} settings tried on {exp['analyst_rows']} "
                              f"scored + {exp['filter_rows']} filtered tokens in {time.time() - t0:.1f}s -> "
                              f"{len(props)} proposals ({solid} solid), report: reports/controller_{self.cfg.mode}_latest.md",
                      proposals=len(props))
            self.next_round = time.time() + self.c.interval_minutes * 60
            self.status("idle")

    def write_report(self) -> str:
        self.report = build_report(self.health, self.exp, self.cfg, self.trials, self.rounds)
        self.report_ts = time.time()
        path = self.cfg.data_dir / "reports" / f"controller_{self.cfg.mode}_latest.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.report, encoding="utf-8")
        return self.report

    def summary(self) -> dict:
        """For the dashboard."""
        return {"health": self.health, "rounds": self.rounds, "trials": self.trials,
                "next_round": self.next_round, "report_ts": self.report_ts,
                "proposals": proposals(self.exp, self.cfg) if self.exp else [],
                "data": {k: self.exp.get(k) for k in ("analyst_rows", "with_path", "filter_rows", "tried")}
                if self.exp else {}}
