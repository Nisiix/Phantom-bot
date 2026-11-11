"""CONTROLLER (Controllore) agent — watches the other agents, tests other settings offline and, when
[controller] auto_apply allows it, applies the improvements it finds and remembers why (desk/controller_memory.py).

It never places an order and makes no API calls (everything comes from memory and the local database).
Its memory (rounds, best variants, streaks, decisions, last report) lives in the database and survives restarts.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections import Counter, deque

from .. import controller_memory as mem
from .. import model as ml
from ..controller import (build_report, health_checks, profit_lock_section, proposals, reason_category,
                          run_experiments)
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
        self.streaks: dict = st.get("streaks", {})
        self.last_applied: dict = st.get("last_applied", {})
        self._announced: set[str] = set()
        self.model_new: dict | None = None
        self.model_res: dict | None = (desk.store.kv_get("ml_model", {}) or {}).get("last") or None
        # Survive restarts: last report, last test round and last checks are shown again right away.
        last = desk.store.kv_get("controller_last", {}) or {}
        self.report, self.report_ts = last.get("report", ""), last.get("report_ts", 0.0)
        self.exp, self.health = last.get("exp"), last.get("health", [])
        self.state.counters["rounds"], self.state.counters["trials"] = self.rounds, self.trials
        self.state.counters["applied"] = sum(d["kind"] == "apply" for d in mem.decisions(desk.store))
        desk.controller = self

    async def run(self) -> None:
        if not self.c.enabled:
            self.status("idle")
            self.emit("info", "Controller disabled ([controller] enabled = false)")
            return
        self.status("working")
        self.emit("info", f"Controller online: checks the agents every {self.c.health_seconds:g}s, tests other "
                          f"settings on logged data every {self.c.interval_minutes:g} min, applies improvements "
                          f"{'by itself' if mem.auto_apply_on(self.cfg) else 'NO (proposals only)'}, no API calls; "
                          f"memory: {self.rounds} rounds, {self.trials} settings tried, "
                          f"{len(mem.decisions(self.desk.store))} decisions"
                          + (f", Controller settings in use: {', '.join(f'{k}={v}' for k, v in self.cfg.controller_overrides.items())}"
                             if self.cfg.controller_overrides else ""))
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
                    "hard_stop": cfg.risk.exit_stop_pct or cfg.risk.hard_stop_pct, "trail_activate": cfg.risk.trail_activate_pct,
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

    def _round(self, reviews: list[dict] | None = None) -> dict:
        """One round in a worker thread: the experiments, then the model retrained on the same rows
        (its result is left in self.model_new for the event loop)."""
        an, fi, rej = self._load()
        exp = run_experiments(self.cfg, an, fi, rej, self.best, reviews=reviews)
        self.model_new = None
        if self.cfg.model.enabled:
            try:
                self.model_new = ml.train_round(an, self.cfg)
            except Exception as e:  # noqa: BLE001 - a model failure must never stop the Controller's other work
                self.model_new = {"t": time.time(), "rows": len(an), "min_rows": self.cfg.model.min_rows,
                                  "verdict": "errore", "why": f"errore di addestramento: {e.__class__.__name__}: {e}"}
        return exp

    def _save_state(self) -> None:
        self.desk.store.kv_set("controller_state", {"rounds": self.rounds, "trials": self.trials, "best": self.best,
                                                    "streaks": self.streaks, "last_applied": self.last_applied})

    # ── decisions: verify earlier ones, apply new ones ──
    def review(self, exp: dict) -> None:
        store = self.desk.store
        by_id = {d["id"]: d for d in mem.decisions(store)}
        for rv in exp.get("reviews", []):
            d = by_id.get(rv["id"])
            if d is None:
                continue
            verdict = mem.judge_review(rv, self.c.review_min_rows)
            store.db.execute("UPDATE controller_decisions SET review=?, review_ts=?, status=? WHERE id=?",
                             (json.dumps(rv), time.time(), verdict or "attiva", d["id"]))
            if verdict == "confermata":
                self.emit("info", f"decision confirmed by {rv['n']} new tokens: {'; '.join(d['changes'])} "
                                  f"({rv['mean']:+.2f} pts/token)")
            elif verdict == "smentita":
                self.revert(d, rv, exp)

    def revert(self, d: dict, rv: dict, exp: dict) -> None:
        """A change the newer data disproved goes back to the previous values (if nothing changed them since)."""
        cur = {k: getattr(getattr(self.cfg, k.split(".")[0]), k.split(".")[1]) for k in (d["after"] or {})}
        if cur != d["after"]:
            return
        triples = [(k.split(".")[0], k.split(".")[1], v) for k, v in (d["before"] or {}).items()]
        before, after = mem.set_values(self.cfg, triples)
        mem.save_overrides(self.cfg, after)
        why = (f"Annullata la decisione #{d['id']} ({'; '.join(d['changes'])}): sui {rv['n']} token arrivati dopo, "
               f"la nuova impostazione ha fatto {rv['mean']:+.2f} punti % per token rispetto alla vecchia "
               f"(intervallo [{rv['lo']:+.2f}, {rv['hi']:+.2f}]). Torno ai valori precedenti e non riproverò "
               "questa modifica.")
        mem.record(self.desk.store, self.cfg, "revert", d["agent"], [f"{k} = {v}" for k, v in after.items()],
                   before, after, None, 0, exp, why)
        self.last_applied[d["agent"]] = time.time()
        self.emit("risk", f"ROLLED BACK: {why}")

    def auto_apply(self, exp: dict, props: list[dict]) -> int:
        if not mem.auto_apply_on(self.cfg):
            return 0
        picked, self.streaks = mem.choose(props, self.cfg, self.streaks, self.last_applied,
                                          mem.refuted_keys(self.desk.store), time.time())
        for p, triples, streak in picked:
            if mem.scout_all_off(self.cfg, triples):
                continue
            before, after = mem.set_values(self.cfg, triples)
            mem.save_overrides(self.cfg, after)
            why = mem.reasoning_text(p, streak, exp, before, after)
            mem.record(self.desk.store, self.cfg, "apply", p["agent"], p["changes"], before, after, p, streak, exp,
                       why, p.get("current"), p.get("variant"))
            self.last_applied[p["agent"]] = time.time()
            self.streaks = {k: v for k, v in self.streaks.items() if not k.startswith(p["agent"] + "|")}
            self.emit("risk", f"APPLIED: {why}")
        return len(picked)

    def flag_entry_trial(self, t: dict | None) -> None:
        """The user's entry-timing trial: announce when its status changes; 'da rimuovere' is a warning."""
        if not t:
            return
        prev = self.desk.store.kv_get("entry_trial_status", "")
        if t["status"] == prev:
            return
        self.desk.store.kv_set("entry_trial_status", t["status"])
        if t["status"] == "in prova":
            return
        msg = (f"entry timing trial ({t['rule']['mode']}): {t['status'].upper()} — {t['mean']:+.2f} pts/token vs "
               f"buying at once on {t['n']} tokens [{t['lo']:+.2f}, {t['hi']:+.2f}]")
        if t["status"].startswith("da rimuovere"):
            self.emit("risk", msg + ". Set [entry] mode = \"now\" in config.toml to remove it")
        else:
            self.emit("info", msg)

    async def experiment_loop(self) -> None:
        while not self.desk.stop.is_set():
            wait = self.next_round - time.time()
            if wait > 0:
                if not await self.sleep(min(wait, 30)):
                    return
                continue
            self.status("working")
            t0 = time.time()
            reviews = mem.pending_reviews(self.desk.store)
            exp = await asyncio.to_thread(self._round, reviews)
            res = self.model_new
            if res is not None:
                self.model_res = res
                gate = self.desk.model
                if gate is not None:
                    msg = gate.update(res, mem.auto_apply_on(self.cfg))
                    if msg:
                        self.emit("risk", msg)
                    self.state.counters["model"] = 1 if gate.active else 0
            self.exp = exp
            self.rounds += 1
            self.trials += exp["tried"]
            self.best = exp["best"]
            self.review(exp)
            props = proposals(exp, self.cfg)
            applied = self.auto_apply(exp, props)
            self.flag_entry_trial(exp.get("entry_trial"))
            self.state.counters["applied"] = self.state.counters.get("applied", 0) + applied
            self._save_state()
            self.write_report()
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

    def _exit_records(self) -> list[dict]:
        try:
            return self.desk.store.q("SELECT data FROM trades UNION ALL SELECT data FROM trades_archive")
        except Exception:  # noqa: BLE001 - report only
            return []

    def write_report(self) -> str:
        self.report = build_report(self.health, self.exp, self.cfg, self.trials, self.rounds,
                                   mem.report_section(self.desk.store, self.cfg)) \
            + "\n\n" + "\n".join(profit_lock_section(self._exit_records(), self.cfg)) \
            + ml.report_section(self.model_res, self.desk.model, self.cfg)
        self.report_ts = time.time()
        self.desk.store.kv_set("controller_last", {"report": self.report, "report_ts": self.report_ts,
                                                   "exp": self.exp, "health": self.health})
        path = self.cfg.data_dir / "reports" / f"controller_{self.cfg.mode}_latest.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.report, encoding="utf-8")
        return self.report

    def summary(self) -> dict:
        """For the dashboard."""
        return {"health": self.health, "rounds": self.rounds, "trials": self.trials,
                "next_round": self.next_round, "report_ts": self.report_ts,
                "proposals": proposals(self.exp, self.cfg) if self.exp else [],
                "auto_apply": self.c.auto_apply, "auto_on": mem.auto_apply_on(self.cfg),
                "overrides": self.cfg.controller_overrides,
                "model": self.desk.model.summary() if self.desk.model is not None else None,
                "decisions": [{k: d[k] for k in ("id", "ts", "agent", "kind", "changes", "status", "reasoning",
                                                 "review")} for d in mem.decisions(self.desk.store, 12)],
                "data": {k: self.exp.get(k) for k in ("analyst_rows", "with_path", "filter_rows", "tried")}
                if self.exp else {}}
