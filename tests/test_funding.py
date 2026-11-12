"""Who funded the creator (user 2026-11-12): block a creator funded by a rugger or by a rugger's funder."""
from __future__ import annotations

import asyncio
import time

from desk.funding import Funding, funder_in_tx
from desk.store import Store


def tx(src: str, dst: str, sol: float = 0.5, kind: str = "transfer") -> dict:
    info = {"source": src, "lamports": int(sol * 1e9)}
    info["newAccount" if kind == "createAccount" else "destination"] = dst
    return {"transaction": {"message": {"instructions": [{"program": "system",
                                                          "parsed": {"type": kind, "info": info}}]}}, "meta": {}}


class Rpc:
    """wallet -> (funder, signatures); a funder of None has no plain funding transfer."""

    def __init__(self, wallets: dict, busy: set = frozenset()) -> None:
        self.wallets, self.busy, self.calls = wallets, busy, 0

    async def call(self, method, params):
        self.calls += 1
        w = params[0]
        if method == "getSignaturesForAddress":
            if w in self.busy:
                return [{"signature": f"{w}-{i}", "blockTime": time.time() - 60} for i in range(1000)]
            n = self.wallets.get(w, (None, 3))[1]
            return [{"signature": f"{w}-{i}", "blockTime": time.time() - 3600} for i in range(n)]
        if method == "getTransaction":
            w = params[0].rsplit("-", 1)[0]
            src = self.wallets.get(w, (None, 0))[0]
            return tx(src, w) if src else {"transaction": {"message": {"instructions": []}}, "meta": {}}
        raise AssertionError(method)


def test_funder_in_tx_reads_transfer_and_create_account():
    assert funder_in_tx(tx("F", "C", 0.2), "C") == ("F", 0.2)
    assert funder_in_tx(tx("F", "C", 1.0, "createAccount"), "C") == ("F", 1.0)
    assert funder_in_tx(tx("F", "X"), "C") == (None, 0.0)


def test_creator_funded_by_a_ruggers_funder_is_blocked(tmp_path):
    st = Store(tmp_path / "p.db")
    st.add_rugger("OLDDEV", "M1", "liquidity collapsed")
    rpc = Rpc({"OLDDEV": ("BOSS", 5), "NEWDEV": ("BOSS", 4), "BOSS": ("CEX", 20)}, busy={"CEX"})
    fd = Funding(st, rpc, hops=2)
    asyncio.run(fd.funder("OLDDEV"))                 # backfill of the ledger
    why, chain = asyncio.run(fd.check("NEWDEV"))
    assert why and "OLDDEV"[:6] in why and chain[0]["funder"] == "BOSS"


def test_exchange_funder_never_links(tmp_path):
    st = Store(tmp_path / "p.db")
    st.add_rugger("OLDDEV", "M1", "liquidity collapsed")
    rpc = Rpc({"OLDDEV": ("CEX", 5), "NEWDEV": ("CEX", 4)}, busy={"CEX"})
    fd = Funding(st, rpc, hops=2)
    asyncio.run(fd.funder("OLDDEV"))
    why, chain = asyncio.run(fd.check("NEWDEV"))
    assert why is None and chain[0]["hub"] and len(chain) == 1


def test_second_hop_and_cache(tmp_path):
    st = Store(tmp_path / "p.db")
    st.add_rugger("BOSS", "M1", "funded a rugging dev")
    rpc = Rpc({"NEWDEV": ("MULE", 4), "MULE": ("BOSS", 2), "BOSS": (None, 9)})
    fd = Funding(st, rpc, hops=2)
    why, _ = asyncio.run(fd.check("NEWDEV"))
    assert why and "BOSS"[:6] in why
    n = rpc.calls
    asyncio.run(fd.check("NEWDEV"))                  # cached in wallet_funders: no new RPC call
    assert rpc.calls == n


def test_old_busy_creator_is_not_judged(tmp_path):
    st = Store(tmp_path / "p.db")
    rpc = Rpc({}, busy={"OLD"})
    why, chain = asyncio.run(Funding(st, rpc).check("OLD"))
    assert why is None and chain[0]["funder"] is None and not chain[0]["complete"]
