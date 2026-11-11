from __future__ import annotations

from dataclasses import dataclass

from ..clients.jupiter import SOL_MINT


@dataclass
class Fill:
    ok: bool
    sol: float = 0.0          # SOL spent (buy) or received (sell), fees included
    tokens: float = 0.0       # UI tokens received (buy) or sold (sell)
    tx: str = ""
    error: str = ""
    route: str = ""           # how it was executed (e.g. "jupiter-v2 ultra/jupiterz", "rpc metis", "paper")


class Broker:
    """A broker quotes (so the Executor can run its checks on the exact order it will send) and fills.

    Default quotes are plain Jupiter quotes. The live broker overrides them with Swap V2 orders, which are
    quote + signed-ready transaction in one call, so the order that passed the checks is the one executed.
    """
    mode = "base"

    async def cash(self) -> float:  # pragma: no cover
        raise NotImplementedError

    async def quote_buy(self, jup, mint: str, lamports: int, slippage_bps: int) -> dict | None:
        return await jup.quote(SOL_MINT, mint, lamports, slippage_bps)

    async def quote_buy_pinned(self, jup, mint: str, lamports: int, slippage_bps: int,
                               dexes: list[str]) -> dict | None:
        """A buy quote restricted to the observed pool's DEX (desk/pools.py). Plain /swap/v1 quote in both modes:
        Swap V2 orders can't be restricted to a DEX, so live sends this one through the /swap/v1 + RPC path."""
        return await jup.quote(SOL_MINT, mint, lamports, slippage_bps, lane="exec", dexes=dexes)

    async def quote_sell(self, jup, mint: str, raw: int, slippage_bps: int | None) -> dict | None:
        # "exec" lane, like a live order: the sale never waits behind Risk's routine quotes (the risk lane spaces
        # its calls 2 s apart: measured 2026-11-11, a dumping token lost 13 points between the stop and the sale).
        return await jup.quote(mint, SOL_MINT, raw, slippage_bps or 300, lane="exec")

    async def buy(self, mint: str, sol_amount: float, decimals: int, quote: dict) -> Fill:  # pragma: no cover
        raise NotImplementedError

    async def sell(self, mint: str, tokens: float, decimals: int, quote: dict) -> Fill:  # pragma: no cover
        raise NotImplementedError

    async def token_holdings(self, mint: str) -> float | None:
        return None
