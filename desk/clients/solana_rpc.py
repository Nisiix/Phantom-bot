"""Minimal Solana JSON-RPC client (on-chain truth for authorities & balances)."""
from __future__ import annotations

import asyncio
import itertools

from .http import Http

TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


class SolanaRPC:
    def __init__(self, http: Http, url: str) -> None:
        self.http = http
        self.url = url
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list | None = None):
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        res = await self.http.post(self.url, json=body, retries=3)
        if res is None:
            raise RuntimeError(f"RPC {method}: empty response")
        if "error" in res:
            raise RuntimeError(f"RPC {method}: {res['error']}")
        return res.get("result")

    async def mint_info(self, mint: str) -> dict | None:
        """Parsed mint account: mintAuthority, freezeAuthority, decimals, supply, extensions."""
        res = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        val = (res or {}).get("value")
        if not val:
            return None
        parsed = (val.get("data") or {}).get("parsed") or {}
        info = dict(parsed.get("info") or {})
        info["program"] = val.get("owner")
        return info

    async def sol_balance(self, owner: str) -> float:
        res = await self.call("getBalance", [owner, {"commitment": "confirmed"}])
        return (res or {}).get("value", 0) / 1e9

    async def token_balance(self, owner: str, mint: str) -> tuple[float, int]:
        """(ui_amount, decimals) summed over all of owner's accounts for mint."""
        # A mint filter works for both SPL Token and Token-2022 accounts.
        total, decimals = 0.0, 0
        res = await self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        for acc in (res or {}).get("value", []):
            ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(ta.get("uiAmountString") or ta.get("uiAmount") or 0)
            decimals = int(ta.get("decimals") or 0)
        return total, decimals

    async def send_raw(self, tx_b64: str) -> str:
        return await self.call(
            "sendTransaction",
            [tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3,
                      "preflightCommitment": "confirmed"}],
        )

    async def confirm(self, sig: str, timeout: float = 75.0) -> tuple[bool, str]:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            res = await self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])
            st = ((res or {}).get("value") or [None])[0]
            if st:
                if st.get("err"):
                    return False, f"on-chain error: {st['err']}"
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True, st["confirmationStatus"]
            await asyncio.sleep(1.5)
        return False, "confirmation timeout"
