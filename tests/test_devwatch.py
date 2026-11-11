"""Creator selling (user's rule 2026-11-11): desk/devwatch.py, Filter dev_sold kill, Risk DEV_SOLD exit."""
from __future__ import annotations

import asyncio
import time

from desk.agents.filter import Filter
from desk.agents.risk import Risk
from desk.config import DevWatchCfg
from desk.devwatch import DevWatch, Group, ata, linked_wallets
from desk.models import Check
from tests.test_rules import make_desk, position

MINT = "AJaJeMXNumLqFHrEth7ey8PDMPdWqMU7nFSmjK8Rpump"
DEV = "FiuKsAYfuC329MoaT36aHHss7aGK3CvnZrgJeS1b8TvS"
PAL = "9KcthoKzMfEb81JGMcnxjMv6RZZMxgBXJjS69XJxGLtw"
T22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class FakeRPC:
    """getMultipleAccounts answered from {wallet: balance}; a wallet missing = no account."""

    def __init__(self, balances: dict[str, float]) -> None:
        self.balances = balances
        self.calls = 0

    async def mint_info(self, mint):
        return {"program": T22}

    async def call(self, method, params):
        assert method == "getMultipleAccounts"
        self.calls += 1
        by_ata = {ata(w, MINT, T22): b for w, b in self.balances.items()}
        return {"value": [None if a not in by_ata else
                          {"data": {"parsed": {"info": {"tokenAmount": {"uiAmountString": str(by_ata[a])}}}}}
                          for a in params[0]]}


def test_ata_matches_chain():
    # Read on mainnet 2026-11-11: the creator's Token-2022 account for this pump.fun mint.
    assert ata(DEV, MINT, T22) == "8fe85kg4TFRMeKMrD32M9P5TBns9TkDXJxXvGCWdfBeP"


def test_linked_wallets_only_networks_with_the_creator():
    graph = [{"network_type": "transfer", "nodes": [{"id": DEV}, {"id": PAL}]},
             {"network_type": "transfer", "nodes": [{"id": "X1"}, {"id": "X2"}]}]
    assert linked_wallets(DEV, graph) == {PAL: "linked (transfer)"}
    assert linked_wallets(None, graph) == {}


def test_group_math_start_internal_moves_and_sales():
    g = Group(MINT, creator=DEV, initial=100.0, supply=1000.0)
    g.add(DEV, "creator")
    assert g.update({DEV: 100.0}) == 0.0
    g.add(PAL, "linked")
    assert g.update({DEV: 40.0, PAL: 60.0}) == 0.0       # moved inside the group: not a sale
    assert round(g.update({DEV: 40.0, PAL: 51.0}), 1) == 9.0
    assert round(g.update({PAL: 45.0}), 1) == 15.0        # 15% of what they held is gone
    g2 = Group(MINT, creator=DEV, initial=100.0)          # sold before we ever looked: the launch buy counts
    g2.add(DEV, "creator")
    assert g2.update({DEV: 0.0}) == 100.0


def test_breach_threshold_and_dust():
    dw = DevWatch(None, None, DevWatchCfg())
    g = Group(MINT, creator=DEV, initial=100.0, supply=1000.0, wallets={DEV: "creator"})
    g.update({DEV: 90.0})
    assert not dw.breached(g)                              # exactly 10%: allowed
    g.update({DEV: 89.0})
    assert dw.breached(g)
    dust = Group(MINT, creator=DEV, initial=0.5, supply=1_000_000.0, wallets={DEV: "creator"})
    dust.update({DEV: 0.0})
    assert not dw.breached(dust)                           # 0.00005% of supply: not judged


def run(coro):
    return asyncio.run(coro)


def test_filter_kills_token_when_creator_sold(tmp_path):
    desk = make_desk(tmp_path)
    rpc = FakeRPC({DEV: 50.0})
    desk.devwatch = DevWatch(desk.store, rpc, desk.cfg.devwatch)
    desk.devwatch.note_create(MINT, DEV, 100.0)

    class Rug:
        async def insider_graph(self, mint):
            return [{"network_type": "transfer", "nodes": [{"id": DEV}, {"id": PAL}]}]
    desk.rug = Rug()
    f = Filter(desk)
    token = {"decimals": 6, "supply": 1000 * 10 ** 6}
    chk = run(f._dev_sold(MINT, DEV, {"tokenProgram": T22}, token, [Check("x", True)]))
    assert not chk.passed and chk.permanent and chk.name == "dev_sold"
    assert PAL in desk.devwatch.groups[MINT].wallets
    # saved: a restart remembers what the group held
    assert desk.store.dev_group(MINT)["initial"] == 100.0
    # a token already dead for another permanent reason is not read again
    assert run(f._dev_sold(MINT, DEV, {}, token, [Check("mint_revoked", False, permanent=True)])) is None


def test_filter_passes_clean_creator(tmp_path):
    desk = make_desk(tmp_path)
    desk.devwatch = DevWatch(desk.store, FakeRPC({DEV: 95.0}), desk.cfg.devwatch)
    desk.devwatch.note_create(MINT, DEV, 100.0)

    class Rug:
        async def insider_graph(self, mint):
            return []
    desk.rug = Rug()
    chk = run(Filter(desk)._dev_sold(MINT, DEV, {}, {"decimals": 6, "supply": 1000 * 10 ** 6}, []))
    assert chk.passed and not chk.permanent


def test_risk_sells_position_when_creator_sold(tmp_path):
    desk = make_desk(tmp_path)
    desk.cfg.devwatch.position_check_seconds = 0.01
    rpc = FakeRPC({DEV: 100.0})
    desk.devwatch = DevWatch(desk.store, rpc, desk.cfg.devwatch)
    g = desk.devwatch.group(MINT, DEV, 1000.0, T22)
    g.initial = 100.0
    pos = position(1.0, 0.7)                               # at a loss: no stop would sell it
    pos.mint = MINT
    desk.portfolio.positions[MINT] = pos
    risk = Risk(desk)

    async def go():
        task = asyncio.create_task(risk.dev_loop())
        await asyncio.sleep(0.05)
        assert desk.exits.empty()                          # creator still holds everything
        rpc.balances[DEV] = 80.0                           # sold 20%
        order = await asyncio.wait_for(desk.exits.get(), 1.0)
        desk.stop.set()
        await task
        return order
    order = run(go())
    assert order.reason == "DEV_SOLD" and order.mint == MINT and "sold 20.0%" in order.detail
