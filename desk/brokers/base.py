from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Fill:
    ok: bool
    sol: float = 0.0          # SOL spent (buy) or received (sell), fees included
    tokens: float = 0.0       # UI tokens received (buy) or sold (sell)
    tx: str = ""
    error: str = ""


class Broker:
    mode = "base"

    async def cash(self) -> float:  # pragma: no cover
        raise NotImplementedError

    async def buy(self, mint: str, sol_amount: float, decimals: int, quote: dict) -> Fill:  # pragma: no cover
        raise NotImplementedError

    async def sell(self, mint: str, tokens: float, decimals: int, quote: dict) -> Fill:  # pragma: no cover
        raise NotImplementedError
