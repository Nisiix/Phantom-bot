"""SQLite persistence. One database per mode (paper.db / live.db) so paper
results can never contaminate live accounting (and vice versa)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL, agent TEXT, kind TEXT, mint TEXT, symbol TEXT, message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts REAL, stage TEXT, outcome TEXT, mint TEXT, symbol TEXT, reason TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_dec_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, mint TEXT, symbol TEXT, mode TEXT,
  entry_ts REAL, exit_ts REAL, cost_sol REAL, proceeds_sol REAL, pnl_sol REAL, pnl_pct REAL,
  peak_pnl_pct REAL, score REAL, entry_reason TEXT, exit_reason TEXT, tx_in TEXT, tx_out TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS positions (mint TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, cash_sol REAL, equity_sol REAL, sol_usd REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY, ts REAL, mode TEXT, mint TEXT, symbol TEXT, score REAL, label TEXT,
  ret_net_pct REAL, rt_cost_pct REAL, gross_pct REAL, p_up REAL, p_down REAL, p_neither REAL, ev_pct REAL);
CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY, start_ts REAL, end_ts REAL, mode TEXT, mint TEXT, symbol TEXT, stage TEXT,
  decision TEXT, reason TEXT, score REAL, traded INTEGER, ref_price REAL, ref_liq REAL, cost_pct REAL,
  desk_label TEXT, desk_hit_s REAL, model_label TEXT, model_hit_s REAL, ret_end_pct REAL, max_pct REAL,
  min_pct REAL, sim_net_pct REAL, p_up REAL, p_down REAL, p_neither REAL);
CREATE INDEX IF NOT EXISTS ix_shadow_end ON shadow(end_ts);
CREATE TABLE IF NOT EXISTS trades_archive (
  id INTEGER PRIMARY KEY, reset_ts REAL, mint TEXT, symbol TEXT, mode TEXT,
  entry_ts REAL, exit_ts REAL, cost_sol REAL, proceeds_sol REAL, pnl_sol REAL, pnl_pct REAL,
  peak_pnl_pct REAL, score REAL, entry_reason TEXT, exit_reason TEXT, tx_in TEXT, tx_out TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS equity_archive (reset_ts REAL, ts REAL, cash_sol REAL, equity_sol REAL, sol_usd REAL);
CREATE TABLE IF NOT EXISTS controller_runs (
  id INTEGER PRIMARY KEY, ts REAL, trials INTEGER, health TEXT, proposals TEXT, report TEXT);
CREATE TABLE IF NOT EXISTS rug_ledger (wallet TEXT PRIMARY KEY, mint TEXT, reason TEXT, ts REAL);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        for col in ("source", "features", "path"):
            try:
                self.db.execute(f"ALTER TABLE shadow ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists

    # ── generic ──
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        cur = self.db.execute(sql, args)
        cols = [c[0] for c in cur.description or []]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self.q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value)))

    # ── writes ──
    def add_event(self, ts, agent, kind, mint, symbol, message, data) -> None:
        self.db.execute(
            "INSERT INTO events(ts,agent,kind,mint,symbol,message,data) VALUES(?,?,?,?,?,?,?)",
            (ts, agent, kind, mint, symbol, message, json.dumps(data, default=str)),
        )

    def add_decision(self, ts, stage, outcome, mint, symbol, reason, data) -> None:
        self.db.execute(
            "INSERT INTO decisions(ts,stage,outcome,mint,symbol,reason,data) VALUES(?,?,?,?,?,?,?)",
            (ts, stage, outcome, mint, symbol, reason, json.dumps(data, default=str)),
        )

    def add_trade(self, t: dict) -> None:
        self.db.execute(
            "INSERT INTO trades(mint,symbol,mode,entry_ts,exit_ts,cost_sol,proceeds_sol,pnl_sol,pnl_pct,"
            "peak_pnl_pct,score,entry_reason,exit_reason,tx_in,tx_out,data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["mint"], t["symbol"], t["mode"], t["entry_ts"], t["exit_ts"], t["cost_sol"], t["proceeds_sol"],
             t["pnl_sol"], t["pnl_pct"], t["peak_pnl_pct"], t["score"], t["entry_reason"], t["exit_reason"],
             t.get("tx_in", ""), t.get("tx_out", ""), json.dumps(t.get("data", {}), default=str)),
        )

    def save_position(self, mint: str, data: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO positions(mint,data) VALUES(?,?)", (mint, json.dumps(data)))

    def delete_position(self, mint: str) -> None:
        self.db.execute("DELETE FROM positions WHERE mint=?", (mint,))

    def load_positions(self) -> list[dict]:
        return [json.loads(r["data"]) for r in self.q("SELECT data FROM positions")]

    def add_equity(self, ts, cash, equity, sol_usd) -> None:
        self.db.execute("INSERT OR REPLACE INTO equity(ts,cash_sol,equity_sol,sol_usd) VALUES(?,?,?,?)",
                        (ts, cash, equity, sol_usd))

    def add_outcome(self, o: dict) -> None:
        self.db.execute(
            "INSERT INTO outcomes(ts,mode,mint,symbol,score,label,ret_net_pct,rt_cost_pct,gross_pct,p_up,p_down,"
            "p_neither,ev_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o["ts"], o["mode"], o["mint"], o["symbol"], o["score"], o["label"], o["ret_net_pct"],
             o["rt_cost_pct"], o["gross_pct"], o["p_up"], o["p_down"], o["p_neither"], o["ev_pct"]))

    def add_shadow(self, r: dict) -> None:
        cols = ",".join(r)
        self.db.execute(f"INSERT INTO shadow({cols}) VALUES({','.join('?' * len(r))})", tuple(r.values()))

    def shadows_between(self, t0: float, t1: float) -> list[dict]:
        return self.q("SELECT * FROM shadow WHERE end_ts>=? AND end_ts<? ORDER BY end_ts", (t0, t1))

    def shadow_samples(self) -> list[dict]:
        return self.q("SELECT * FROM shadow WHERE stage='analyst' AND traded=0 AND score IS NOT NULL")

    def outcomes(self) -> list[dict]:
        return self.q("SELECT * FROM outcomes ORDER BY ts")

    def add_rugger(self, wallet: str, mint: str, reason: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO rug_ledger(wallet,mint,reason,ts) VALUES(?,?,?,?)",
                        (wallet, mint, reason, time.time()))

    def ruggers(self) -> dict[str, dict]:
        return {r["wallet"]: r for r in self.q("SELECT * FROM rug_ledger")}

    def rugger(self, wallet: str | None) -> dict | None:
        """Single indexed lookup (the ledger only grows: never load all of it per token)."""
        if not wallet:
            return None
        rows = self.q("SELECT * FROM rug_ledger WHERE wallet=?", (wallet,))
        return rows[0] if rows else None

    def shadow_research_rows(self, stage: str = "analyst", since: float = 0) -> list[dict]:
        return self.q("SELECT * FROM shadow WHERE stage=? AND end_ts>=? AND features IS NOT NULL ORDER BY end_ts",
                      (stage, since))

    # ── paper resets: the record before a reset still counts for the live gate ──
    def archive_and_clear_trades(self, reset_ts: float) -> None:
        cols = ("mint,symbol,mode,entry_ts,exit_ts,cost_sol,proceeds_sol,pnl_sol,pnl_pct,peak_pnl_pct,score,"
                "entry_reason,exit_reason,tx_in,tx_out,data")
        self.db.execute(f"INSERT INTO trades_archive(reset_ts,{cols}) SELECT ?,{cols} FROM trades", (reset_ts,))
        self.db.execute("INSERT INTO equity_archive(reset_ts,ts,cash_sol,equity_sol,sol_usd) "
                        "SELECT ?,ts,cash_sol,equity_sol,sol_usd FROM equity", (reset_ts,))
        for table in ("trades", "positions", "equity"):
            self.db.execute(f"DELETE FROM {table}")

    def archived_trades(self) -> list[dict]:
        return self.q("SELECT * FROM trades_archive ORDER BY exit_ts")

    def equity_segments(self) -> list[list[float]]:
        """Equity curve per account period (one per paper reset, then the current one)."""
        segs: dict[float, list[float]] = {}
        for r in self.q("SELECT reset_ts,equity_sol FROM equity_archive ORDER BY reset_ts,ts"):
            segs.setdefault(r["reset_ts"], []).append(r["equity_sol"])
        return list(segs.values()) + [[e["equity_sol"] for e in self.equity_series(0)]]

    # ── reads ──
    def trades_between(self, t0: float, t1: float) -> list[dict]:
        return self.q("SELECT * FROM trades WHERE exit_ts>=? AND exit_ts<? ORDER BY exit_ts", (t0, t1))

    def all_trades(self) -> list[dict]:
        return self.q("SELECT * FROM trades ORDER BY exit_ts")

    def equity_series(self, since: float = 0) -> list[dict]:
        return self.q("SELECT ts,cash_sol,equity_sol,sol_usd FROM equity WHERE ts>=? ORDER BY ts", (since,))

    def funnel_since(self, since: float) -> dict:
        """Distinct tokens per pipeline stage from the decision log (survives restarts, unlike agent counters)."""
        rows = self.q("SELECT stage, outcome, COUNT(DISTINCT mint) AS tokens, COUNT(*) AS n FROM decisions "
                      "WHERE ts>=? GROUP BY stage, outcome", (since,))
        by = {(r["stage"], r["outcome"]): r for r in rows}
        seen = self.q("SELECT COUNT(DISTINCT mint) AS n FROM decisions WHERE ts>=? AND stage='filter'", (since,))
        return {"seen": seen[0]["n"] if seen else 0,
                "passed": by.get(("filter", "pass"), {}).get("tokens", 0),
                "approved": by.get(("analyst", "approve"), {}).get("tokens", 0),
                "bought": by.get(("executor", "buy"), {}).get("n", 0),
                "filter_checks": sum(r["n"] for r in rows if r["stage"] == "filter")}

    def decisions_between(self, t0: float, t1: float) -> list[dict]:
        return self.q("SELECT * FROM decisions WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1))

    def events_between(self, t0: float, t1: float, kinds: tuple[str, ...] | None = None) -> list[dict]:
        if kinds:
            marks = ",".join("?" * len(kinds))
            return self.q(f"SELECT * FROM events WHERE ts>=? AND ts<? AND kind IN ({marks}) ORDER BY ts",
                          (t0, t1, *kinds))
        return self.q("SELECT * FROM events WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1))
