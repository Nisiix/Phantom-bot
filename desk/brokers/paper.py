"""Paper broker: fills at REAL live Jupiter quotes, then applies a conservative
haircut for latency/slippage plus network + priority fees. No money moves."""
from __future__ import annotations

from .base import Broker, Fill

NETWORK_FEE_SOL = 0.000005
PRIORITY_FEE_SOL = 0.0001     # typical landed priority fee estimate
LATENCY_HAIRCUT = 0.006       # fills assumed 0.6% worse than the quote


class PaperBroker(Broker):
    mode = "paper"

    def __init__(self, portfolio) -> None:
        self.portfolio = portfolio

    async def cash(self) -> float:
        return self.portfolio.cash_sol

    async def buy(self, mint: str, sol_amount: float, decimals: int, quote: dict) -> Fill:
        out_raw = int(quote["outAmount"])
        tokens = out_raw / 10 ** decimals * (1 - LATENCY_HAIRCUT)
        spent = int(quote["inAmount"]) / 1e9 + NETWORK_FEE_SOL + PRIORITY_FEE_SOL
        if tokens <= 0:
            return Fill(False, error="quote returned zero tokens")
        return Fill(True, sol=spent, tokens=tokens, tx="paper", route="paper")

    async def sell(self, mint: str, tokens: float, decimals: int, quote: dict) -> Fill:
        received = int(quote["outAmount"]) / 1e9 * (1 - LATENCY_HAIRCUT) - NETWORK_FEE_SOL - PRIORITY_FEE_SOL
        return Fill(True, sol=max(0.0, received), tokens=tokens, tx="paper", route="paper")
