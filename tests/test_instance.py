"""2026-11-14: one desk per data folder, and a position already sold never comes back."""
import json
import time

import pytest

from desk.instance import AlreadyRunning, InstanceLock
from desk.portfolio import Portfolio
from desk.store import Store


def test_second_desk_on_the_same_data_is_refused(tmp_path):
    a = InstanceLock(tmp_path, "paper").acquire()
    with pytest.raises(AlreadyRunning) as e:
        InstanceLock(tmp_path, "paper").acquire()
    assert "già in esecuzione" in str(e.value) and "PID" in str(e.value)
    assert InstanceLock(tmp_path, "live").acquire()          # another mode has its own database
    a.release()
    InstanceLock(tmp_path, "paper").acquire().release()      # free again once the first one stops


def test_sold_position_written_back_is_dropped_at_start(tmp_path):
    st = Store(tmp_path / "paper.db")
    entry = time.time() - 3600
    pos = {"mint": "Mpump", "symbol": "Mustache", "tokens": 1000, "decimals": 6, "cost_sol": 0.965,
           "entry_price_sol": 0.000965, "entry_ts": entry, "entry_liquidity_usd": 30000, "exit_cost_pct": 1.0,
           "score": 0.7, "entry_reason": "t", "mark_value_sol": 0.15, "peak_value_sol": 1.0}
    st.save_position("Mpump", pos)
    st.save_position("Upump", {**pos, "mint": "Upump", "symbol": "UP"})
    st.add_trade({"mint": "Mpump", "symbol": "Mustache", "mode": "paper", "entry_ts": entry, "exit_ts": entry + 60,
                  "cost_sol": 0.965, "proceeds_sol": 0.168, "pnl_sol": -0.797, "pnl_pct": -82.6, "peak_pnl_pct": 10,
                  "score": 0.7, "entry_reason": "t", "exit_reason": "MANUAL"})
    # an earlier trade on the same token (another entry) does not count
    st.add_trade({"mint": "Upump", "symbol": "UP", "mode": "paper", "entry_ts": entry - 9000, "exit_ts": entry - 8000,
                  "cost_sol": 1, "proceeds_sol": 1, "pnl_sol": 0, "pnl_pct": 0, "peak_pnl_pct": 0,
                  "score": 0.7, "entry_reason": "t", "exit_reason": "PROFIT_LOCK"})
    pf = Portfolio(st, "paper", 10.0)
    assert set(pf.positions) == {"Upump"} and pf.ghosts == ["Mustache"]
    assert [json.loads(r["data"])["symbol"] for r in st.q("SELECT data FROM positions")] == ["UP"]
