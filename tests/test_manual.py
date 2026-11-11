"""Sell on the owner's request and paper reset at the next start (2026-11-12)."""
from __future__ import annotations

import asyncio
import json

import run
from desk.app import request_sell, reset_pending, sell_flag, sell_watch
from desk.agents.risk import Risk
from tests.test_rules import make_desk, position


def test_sell_flag_queues_a_manual_exit(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)
    desk.portfolio.open(position())
    request_sell(desk.cfg, ["M", "NOPE"])

    async def go():
        task = asyncio.create_task(sell_watch(desk, risk, sell_flag(desk.cfg), poll=0.01))
        order = await asyncio.wait_for(desk.exits.get(), 2)
        desk.stop.set()
        await task
        return order
    order = asyncio.run(go())
    assert order.mint == "M" and order.reason == "MANUAL" and "M" in risk.exiting
    assert not sell_flag(desk.cfg).exists()


def test_pending_reset_uses_usd_capital_and_keeps_learning(tmp_path):
    desk = make_desk(tmp_path)
    cfg = desk.cfg
    from desk.store import Store
    st = Store(cfg.data_dir / "paper.db")
    st.kv_set("sol_usd", 80.0)
    st.kv_set("cash_sol", 3.0)
    st.kv_set("risk_state", {"history": {"X": 1}})
    reset_pending(cfg).write_text(json.dumps({"usd": 1000}), encoding="utf-8")
    run.pending_reset(cfg)
    st2 = Store(cfg.data_dir / "paper.db")
    assert st2.kv_get("cash_sol") == 12.5 and st2.kv_get("starting_cash") == 12.5
    assert st2.kv_get("risk_state")["history"] == {"X": 1}
    assert not reset_pending(cfg).exists()
