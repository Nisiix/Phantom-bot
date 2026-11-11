"""Restart on request (python run.py restart): no new buys, wait until flat, exit to be started again."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from desk.app import RESTART_CODE, request_restart, restart_flag, restart_watch
from desk.agents.risk import Risk
from tests.test_rules import make_desk, position


def _desk(positions=None):
    events = []
    return SimpleNamespace(stop=asyncio.Event(), restart=False, restart_pending=False, in_flight=set(),
                           portfolio=SimpleNamespace(positions=dict(positions or {})),
                           hub=SimpleNamespace(emit=events.append)), events


def test_restart_waits_until_no_position_is_open(tmp_path):
    cfg = SimpleNamespace(data_dir=tmp_path)
    flag = restart_flag(cfg)
    flag.write_text("stale request from a previous run")

    async def go():
        desk, events = _desk({"M": object()})
        task = asyncio.create_task(restart_watch(desk, flag, poll=0.01))
        await asyncio.sleep(0.05)
        assert not flag.exists() and not desk.restart_pending          # the stale file was dropped at start
        request_restart(cfg)
        await asyncio.sleep(0.05)
        assert desk.restart_pending and not desk.stop.is_set()         # a position is open: wait, no new buys
        desk.portfolio.positions.clear()
        await asyncio.wait_for(task, 1)
        assert desk.restart and desk.stop.is_set() and not flag.exists()
        assert any("restart requested" in e.message for e in events)
    asyncio.run(go())


def test_restart_now_and_withdrawn_request(tmp_path):
    cfg = SimpleNamespace(data_dir=tmp_path)
    flag = restart_flag(cfg)

    async def go():
        desk, _ = _desk({"M": object()})
        task = asyncio.create_task(restart_watch(desk, flag, poll=0.01))
        await asyncio.sleep(0.03)
        request_restart(cfg)
        await asyncio.sleep(0.05)
        flag.unlink()                                                  # withdrawn: buying again
        await asyncio.sleep(0.05)
        assert not desk.restart_pending and not desk.stop.is_set()
        request_restart(cfg, now=True)                                 # at once, even with a position open
        await asyncio.wait_for(task, 1)
        assert desk.restart
        desk2, _ = _desk({"M": object()})                              # the wait has a limit
        task = asyncio.create_task(restart_watch(desk2, flag, poll=0.01))
        await asyncio.sleep(0.03)
        request_restart(cfg, max_wait_min=0.001)
        await asyncio.wait_for(task, 1)
        assert desk2.restart
    asyncio.run(go())


def test_no_new_buys_while_a_restart_is_pending(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)
    assert risk.approve_entry("New")[0]
    desk.restart_pending = True
    ok, why = risk.approve_entry("New")
    assert not ok and "restart" in why and RESTART_CODE == 75
