"""Jupiter aggregator: quotes (paper fills + checks) and swap transactions (live)."""
from __future__ import annotations

from .http import Http

BASE = "https://lite-api.jup.ag"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000


class Jupiter:
    def __init__(self, http: Http) -> None:
        self.http = http

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> dict | None:
        if amount_raw <= 0:
            return None
        q = await self.http.get(
            f"{BASE}/swap/v1/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(int(amount_raw)),
                "slippageBps": str(int(slippage_bps)),
                "restrictIntermediateTokens": "true",
            },
            retries=2,
        )
        if not q or "outAmount" not in q:
            return None
        return q

    async def swap_tx(self, quote: dict, user_pubkey: str, max_priority_lamports: int) -> str:
        res = await self.http.post(
            f"{BASE}/swap/v1/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": user_pubkey,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "dynamicSlippage": False,
                "prioritizationFeeLamports": {
                    "priorityLevelWithMaxLamports": {
                        "maxLamports": int(max_priority_lamports),
                        "priorityLevel": "veryHigh",
                    }
                },
            },
            retries=2,
        )
        if not res or "swapTransaction" not in res:
            raise RuntimeError(f"Jupiter swap build failed: {res}")
        return res["swapTransaction"]

    async def sol_usd(self) -> float:
        data = await self.http.get(f"{BASE}/price/v3", params={"ids": SOL_MINT}, retries=2) or {}
        return float((data.get(SOL_MINT) or {}).get("usdPrice") or 0.0)
