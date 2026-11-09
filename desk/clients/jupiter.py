"""Jupiter aggregator: quotes (paper fills + checks) and swap execution (live).

Live execution uses Jupiter's Swap V2 order/execute flow (the successor of Ultra):
  GET  /swap/v2/order    quote + ready-to-sign transaction in one call. Without slippageBps it runs in
                         "ultra" mode: real-time slippage estimate (RTSE), RFQ market makers (JupiterZ)
                         compete with on-chain routes, MEV-protected landing, gasless when possible.
                         With slippageBps it runs in "manual" mode at exactly that slippage.
  POST /swap/v2/execute  Jupiter lands the signed transaction and returns the exact amounts swapped.
The older /swap/v1/quote + /swap/v1/swap path stays as a fallback (execution = "rpc").
"""
from __future__ import annotations

from .http import Http

BASE = "https://lite-api.jup.ag"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000


class Jupiter:
    def __init__(self, http: Http) -> None:
        self.http = http

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int,
                    lane: str = "") -> dict | None:
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
            lane=lane,
        )
        if not q or "outAmount" not in q:
            return None
        return q

    async def swap_tx(self, quote: dict, user_pubkey: str, max_priority_lamports: int) -> tuple[str, int]:
        """Legacy (Metis) path: build an unsigned swap. Returns (tx_b64, lastValidBlockHeight)."""
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
            lane="exec",
        )
        if not res or "swapTransaction" not in res:
            raise RuntimeError(f"Jupiter swap build failed: {res}")
        return res["swapTransaction"], int(res.get("lastValidBlockHeight") or 0)

    async def order(self, input_mint: str, output_mint: str, amount_raw: int, taker: str,
                    slippage_bps: int | None = None) -> dict | None:
        """Swap V2 order. slippage_bps=None -> Jupiter picks slippage per trade (RTSE) and RFQ may fill."""
        if amount_raw <= 0:
            return None
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount_raw)),
                  "taker": taker}
        if slippage_bps is not None:
            params["slippageBps"] = str(int(slippage_bps))
        o = await self.http.get(f"{BASE}/swap/v2/order", params=params, retries=1, lane="exec")
        if o and not o.get("transaction") and (o.get("errorMessage") or o.get("error")):
            # e.g. "Insufficient funds": a real reason, not a missing route
            raise RuntimeError(f"Jupiter order refused: {o.get('errorMessage') or o.get('error')}")
        if not o or "outAmount" not in o or not o.get("transaction") or not o.get("requestId"):
            return None
        return o

    async def execute(self, signed_tx_b64: str, request_id: str) -> dict:
        """Jupiter lands the transaction. Re-sending the same signed tx + requestId is safe (same signature)."""
        res = await self.http.post(f"{BASE}/swap/v2/execute",
                                   json={"signedTransaction": signed_tx_b64, "requestId": request_id},
                                   retries=2, lane="exec", timeout=60.0)
        return res or {}

    async def sol_usd(self) -> float:
        data = await self.http.get(f"{BASE}/price/v3", params={"ids": SOL_MINT}, retries=2) or {}
        return float((data.get(SOL_MINT) or {}).get("usdPrice") or 0.0)
