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
CREATE TABLE IF NOT EXISTS rug_ledger (wallet TEXT PRIMARY KEY, mint TEXT, reason TEXT, ts REAL);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

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

    def add_rugger(self, wallet: str, mint: str, reason: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO rug_ledger(wallet,mint,reason,ts) VALUES(?,?,?,?)",
                        (wallet, mint, reason, time.time()))

    def ruggers(self) -> dict[str, dict]:
        return {r["wallet"]: r for r in self.q("SELECT * FROM rug_ledger")}

    # ── reads ──
    def trades_between(self, t0: float, t1: float) -> list[dict]:
        return self.q("SELECT * FROM trades WHERE exit_ts>=? AND exit_ts<? ORDER BY exit_ts", (t0, t1))

    def all_trades(self) -> list[dict]:
        return self.q("SELECT * FROM trades ORDER BY exit_ts")

    def equity_series(self, since: float = 0) -> list[dict]:
        return self.q("SELECT ts,cash_sol,equity_sol,sol_usd FROM equity WHERE ts>=? ORDER BY ts", (since,))

    def decisions_between(self, t0: float, t1: float) -> list[dict]:
        return self.q("SELECT * FROM decisions WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1))

    def events_between(self, t0: float, t1: float, kinds: tuple[str, ...] | None = None) -> list[dict]:
        if kinds:
            marks = ",".join("?" * len(kinds))
            return self.q(f"SELECT * FROM events WHERE ts>=? AND ts<? AND kind IN ({marks}) ORDER BY ts",
                          (t0, t1, *kinds))
        return self.q("SELECT * FROM events WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1))
