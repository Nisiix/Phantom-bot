"""WHALE CHECKER (user 2026-11-12) — the agent that runs the smart-wallet engine (desk/smartwallets.py).

  * reads on-chain, with plain public RPC, the creator and the first trades of every token the desk follows
    (open positions, the Analyst's tokens, then the newest shadow follow-ups) and of the past ones (winners first);
  * scores the wallets that keep buying early the tokens that reach $100k of real market cap;
  * every minute, checks whether one of those wallets holds a token the Analyst is following or an open position.

It reports to the Analyst (user 2026-11-12): for every token the Analyst scores, how many smart wallets hold it.
The Analyst uses that report only once the data is sufficient ([smart_wallets] ready_min_*): a bonus on the score
that can carry the token over the threshold to the Executor. Without sufficient data the Analyst ignores it. The
Whale checker never buys, sells or blocks by itself.

It also answers the Executor (user 2026-11-12, desk/whale_entry.py): the smart wallets' own buy price for a token
(the Executor buys only within +30% of it) and, every 30 s while the Executor waits, whether they sold.
"""
from __future__ import annotations

import asyncio

from .base import Agent


class WhaleChecker(Agent):
    name = "Whale checker"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.sw = None
        self._was_ready = None

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
        """Tokens whose smart holders are checked every minute: open positions and the Analyst's tokens. Then
        (user 2026-11-14) the newest tokens still in the graduation wait, only while smart wallets are known:
        measured in their own tables, never bought earlier because of it (SmartWallets.watch_wait)."""
        out = {m: (m, getattr(p, "symbol", ""), "position") for m, p in list(self.desk.portfolio.positions.items())}
        for s in self._shadow():
            if s.stage == "analyst" and s.mint not in out:
                out[s.mint] = (s.mint, s.symbol, "analyst")
        cap = int(self.cfg.smart_wallets.watch_wait_max_tokens)
        sh = self.desk.shadow
        if cap > 0 and sh is not None and self.sw is not None and self.sw.smart:
            waiting = sorted(list(getattr(sh, "deferred", {}).values()), key=lambda d: -d.get("grad", 0))
            for d in [d for d in waiting if d["mint"] not in out][:cap]:
                out[d["mint"]] = (d["mint"], d.get("symbol", ""), "wait")
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
                          f"{len(self.sw.smart)} smart wallets known")
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
            cnt["wait_checks"], cnt["wait_hits"] = s.get("wait_checks", 0), s.get("wait_hits", 0)
            cnt["reports"] = s.get("reports", 0)
            r = self.sw.readiness()
            if r.get("ready") != self._was_ready:
                self._was_ready = r.get("ready")
                self.emit("info", ("data sufficient: the Analyst now uses the smart wallets (" + r["why"] + ")")
                          if r["ready"] else f"the Analyst ignores the smart wallets for now ({r['why']})")
            if not await self.sleep(5):
                return
