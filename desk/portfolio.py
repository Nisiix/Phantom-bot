"""Portfolio: cash, open positions, realized trades. The single source of truth for balance."""
from __future__ import annotations

import time
from dataclasses import fields

from .models import Position
from .store import Store


class AveragingDownError(RuntimeError):
    pass


class Portfolio:
    def __init__(self, store: Store, mode: str, starting_cash: float) -> None:
        self.store = store
        self.mode = mode
        self.cash_sol: float = store.kv_get("cash_sol", starting_cash)
        self.starting_cash: float = store.kv_get("starting_cash", starting_cash)
        store.kv_set("starting_cash", self.starting_cash)
        self.positions: dict[str, Position] = {}
        names = {f.name for f in fields(Position)}
        for d in store.load_positions():
            self.positions[d["mint"]] = Position(**{k: v for k, v in d.items() if k in names})
        self.sol_usd: float = store.kv_get("sol_usd", 0.0)
        self.realized_sol: float = store.kv_get("realized_sol", 0.0)

    # ── valuation ──
    @property
    def positions_value_sol(self) -> float:
        return sum(p.mark_value_sol for p in self.positions.values())

    @property
    def equity_sol(self) -> float:
        return self.cash_sol + self.positions_value_sol

    @property
    def total_pnl_sol(self) -> float:
        return self.equity_sol - self.starting_cash

    def set_cash(self, sol: float) -> None:
        self.cash_sol = sol
        self.store.kv_set("cash_sol", sol)

    def set_sol_usd(self, px: float) -> None:
        if px > 0:
            self.sol_usd = px
            self.store.kv_set("sol_usd", px)

    # ── lifecycle ──
    def open(self, pos: Position) -> None:
        if pos.mint in self.positions:
            # Structural guarantee: no averaging down / no adding to a position.
            raise AveragingDownError(f"position already open in {pos.symbol}")
        self.positions[pos.mint] = pos
        self.set_cash(self.cash_sol - pos.cost_sol)
        self.persist(pos)

    def persist(self, pos: Position) -> None:
        # A position closed while another agent was awaiting must not be written back (ghost position
        # resurrected on the next restart).
        if self.positions.get(pos.mint) is pos:
            self.store.save_position(pos.mint, pos.to_dict())

    def close(self, mint: str, proceeds_sol: float, reason: str, tx_out: str = "", extra: dict | None = None) -> dict:
        pos = self.positions.pop(mint)
        self.store.delete_position(mint)
        self.set_cash(self.cash_sol + proceeds_sol)
        pnl = proceeds_sol - pos.cost_sol
        self.realized_sol += pnl
        self.store.kv_set("realized_sol", self.realized_sol)
        trade = {
            "mint": mint, "symbol": pos.symbol, "mode": self.mode,
            "entry_ts": pos.entry_ts, "exit_ts": time.time(),
            "cost_sol": pos.cost_sol, "proceeds_sol": proceeds_sol, "pnl_sol": pnl,
            "pnl_pct": (proceeds_sol / pos.cost_sol - 1) * 100 if pos.cost_sol else 0.0,
            "peak_pnl_pct": pos.peak_pnl_pct, "score": pos.score,
            "entry_reason": pos.entry_reason, "exit_reason": reason,
            "tx_in": pos.tx_in, "tx_out": tx_out, "data": extra or {},
        }
        self.store.add_trade(trade)
        return trade
