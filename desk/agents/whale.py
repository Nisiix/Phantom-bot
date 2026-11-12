"""WHALE CHECKER (user 2026-11-12) — the agent that runs the smart-wallet engine (desk/smartwallets.py).

  * reads on-chain, with plain public RPC, the creator and the first trades of every token the desk follows
    (open positions, the Analyst's tokens, then the newest shadow follow-ups) and of the past ones (winners first);
  * scores the wallets that keep buying early the tokens that reach $200k of real market cap;
  * every minute, checks whether one of those wallets holds a token the Analyst is following or an open position.

OBSERVATION ONLY: its hits are stored (`sw_hits`) and shown in the feed, nothing buys, sells or blocks on them until
the user decides to use them.
"""
from __future__ import annotations

import asyncio

from .base import Agent


class WhaleChecker(Agent):
    name = "Whale checker"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.sw = None

    def _shadow(self) -> list:
        sh = self.desk.shadow
        return list(sh.active.values()) if sh is not None else []

    def followed(self) -> list[tuple[str, str]]:
        """Tokens to read once: open positions, the Analyst's tokens, then the newest follow-ups (a younger token
        has a shorter history to page back through)."""
        pos = [(m, getattr(p, "symbol", "")) for m, p in list(self.desk.portfolio.positions.items())]
        act = sorted(self._shadow(), key=lambda s: -s.start)
        return pos + [(s.mint, s.symbol) for s in act if s.stage == "analyst"] + \
            [(s.mint, s.symbol) for s in act if s.stage != "analyst"]

    def watched(self) -> list[tuple[str, str, str]]:
        """Tokens whose smart holders are checked every minute: open positions and the Analyst's tokens."""
        out = {m: (m, getattr(p, "symbol", ""), "position") for m, p in list(self.desk.portfolio.positions.items())}
        for s in self._shadow():
            if s.stage == "analyst" and s.mint not in out:
                out[s.mint] = (s.mint, s.symbol, "analyst")
        return list(out.values())

    async def run(self) -> None:
        c = self.cfg.smart_wallets
        if not c.enabled or self.desk.rpc is None:
            self.status("halted")
            self.emit("info", "Whale checker off ([smart_wallets] enabled = false)")
            return
        from ..smartwallets import SmartWallets
        self.sw = SmartWallets(self.desk.store, self.desk.rpc, self.desk.dex, c, self.emit)
        self.desk.smart = self.sw
        self.status("working")
        self.emit("info", f"Whale checker online: first {c.scan_trades} trades of every followed token read on-chain, "
                          f"win = ${float(c.win_mcap_usd) / 1000:.0f}k real market cap, "
                          f"{len(self.sw.smart)} smart wallets known (observation only)")
        await asyncio.gather(self.supervised("whale-engine", self.engine), self.supervised("whale-stats", self.stats))

    async def engine(self) -> None:
        await self.sw.loop(self.followed, self.watched, self.desk.stop)

    async def stats(self) -> None:
        while not self.desk.stop.is_set():
            s = self.sw.stats
            cnt = self.state.counters
            cnt["wallets"] = len(self.sw.smart)
            cnt["scanned"], cnt["backfilled"], cnt["hits"] = s["scanned"], s["backfilled"], s["hits"]
            cnt["too_long"], cnt["rpc_errors"] = s["too_long"], s["errors"]
            cnt["watching"] = len(self.watched())
            if not await self.sleep(5):
                return
