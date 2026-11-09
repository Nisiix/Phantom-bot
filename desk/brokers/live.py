"""Live broker: real swaps through Jupiter, signed locally.

The private key is read from WALLET_PRIVATE_KEY (.env) and never leaves this process: Jupiter builds an
unsigned transaction, we sign it here, and only the signed transaction is sent.

Two execution paths (config [executor].execution):
  * "jupiter_v2" (default): Swap V2 /order + /execute. One call returns quote + transaction, Jupiter lands
    it (its own sender, MEV protected) and answers with the exact amounts swapped. Entries/first exit try
    use RTSE slippage and RFQ quotes; exit retries use explicit, widening slippage.
  * "rpc": legacy /swap/v1 build, sent through YOUR RPC with skipPreflight + our own rebroadcast loop.
    Use it with a private/staked RPC (Helius, Triton, QuickNode...) if you prefer your own landing.
Both measure the fill per transaction (Jupiter's result or the transaction metadata), never from wallet
balance deltas, so a buy and a sell can be in flight at the same time without mixing up their fills.
"""
from __future__ import annotations

import base64
import json

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from ..clients.jupiter import SOL_MINT, Jupiter
from ..clients.solana_rpc import SolanaRPC
from .base import Broker, Fill


def load_keypair(secret: str) -> Keypair:
    secret = secret.strip()
    if not secret:
        raise ValueError("WALLET_PRIVATE_KEY is empty")
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


def sign_swap(tx_b64: str, kp: Keypair) -> str:
    """Sign our slot only. Gasless/RFQ orders carry other signers (fee payer / market maker): keep theirs."""
    unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = unsigned.message
    n = msg.header.num_required_signatures
    signers = list(msg.account_keys[:n])
    if n == 1 and signers[0] == kp.pubkey():
        signed = VersionedTransaction(msg, [kp])
    else:
        sigs = list(unsigned.signatures)
        sigs[signers.index(kp.pubkey())] = kp.sign_message(to_bytes_versioned(msg))
        signed = VersionedTransaction.populate(msg, sigs)
    return base64.b64encode(bytes(signed)).decode()


def order_fees_sol(order: dict, taker: str) -> float:
    """Network/priority/rent costs of a V2 order that the taker (we) pays, in SOL."""
    lam = 0
    for k in ("signatureFee", "prioritizationFee", "rentFee"):
        if order.get(f"{k}Payer") == taker:
            lam += int(order.get(f"{k}Lamports") or 0)
    return lam / 1e9


class LiveBroker(Broker):
    mode = "live"

    def __init__(self, rpc: SolanaRPC, jup: Jupiter, secret: str, max_priority_lamports: int,
                 fee_reserve_sol: float, execution: str = "jupiter_v2") -> None:
        self.rpc = rpc
        self.jup = jup
        self.kp = load_keypair(secret)
        self.pubkey = str(self.kp.pubkey())
        self.max_priority = max_priority_lamports
        self.reserve = fee_reserve_sol
        self.execution = execution

    @property
    def v2(self) -> bool:
        return self.execution == "jupiter_v2"

    async def cash(self) -> float:
        return max(0.0, await self.rpc.sol_balance(self.pubkey) - self.reserve)

    # ── quotes = executable orders ──
    async def quote_buy(self, jup, mint: str, lamports: int, slippage_bps: int) -> dict | None:
        if not self.v2:
            return await super().quote_buy(jup, mint, lamports, slippage_bps)
        return await self.jup.order(SOL_MINT, mint, lamports, self.pubkey)          # RTSE + RFQ

    async def quote_sell(self, jup, mint: str, raw: int, slippage_bps: int | None) -> dict | None:
        if not self.v2:
            return await super().quote_sell(jup, mint, raw, slippage_bps)
        return await self.jup.order(mint, SOL_MINT, raw, self.pubkey, slippage_bps)

    # ── execution ──
    async def _execute_v2(self, order: dict) -> tuple[bool, dict, str]:
        res = await self.jup.execute(sign_swap(order["transaction"], self.kp), order["requestId"])
        ok = str(res.get("status", "")).lower() == "success"
        err = "" if ok else f"{res.get('code', '?')}: {res.get('error') or res.get('status') or 'execute failed'}"
        return ok, res, err

    async def _execute_rpc(self, quote: dict) -> tuple[bool, str, str]:
        tx_b64, lvbh = await self.jup.swap_tx(quote, self.pubkey, self.max_priority)
        return await self.rpc.send_and_confirm(sign_swap(tx_b64, self.kp), lvbh)

    def _route(self, q: dict) -> str:
        if self.v2:
            return f"jupiter-v2 {q.get('mode', '?')}/{q.get('router', '?')}"
        return "rpc metis"

    async def buy(self, mint: str, sol_amount: float, decimals: int, quote: dict) -> Fill:
        route = self._route(quote)
        try:
            if self.v2:
                ok, res, err = await self._execute_v2(quote)
                sig = res.get("signature", "")
                if not ok:
                    return Fill(False, tx=sig, error=err, route=route)
                if res.get("outputAmountResult") and res.get("inputAmountResult"):
                    return Fill(True, sol=int(res["inputAmountResult"]) / 1e9 + order_fees_sol(quote, self.pubkey),
                                tokens=int(res["outputAmountResult"]) / 10 ** decimals, tx=sig, route=route)
            else:
                ok, sig, detail = await self._execute_rpc(quote)
                if not ok:
                    return Fill(False, tx=sig, error=detail, route=route)
        except Exception as e:  # noqa: BLE001
            return Fill(False, error=f"swap failed: {e}", route=route)
        fill = await self.rpc.tx_fill(sig, self.pubkey, mint)
        if fill is None:          # landed, metadata not served yet: book the quote rather than orphan tokens
            return Fill(True, sol=int(quote["inAmount"]) / 1e9, tokens=int(quote["outAmount"]) / 10 ** decimals,
                        tx=sig, route=route + " (estimated from quote)")
        sol, tok = fill
        return Fill(True, sol=-sol, tokens=tok, tx=sig, route=route)

    async def sell(self, mint: str, tokens: float, decimals: int, quote: dict) -> Fill:
        route = self._route(quote)
        try:
            if self.v2:
                ok, res, err = await self._execute_v2(quote)
                sig = res.get("signature", "")
                if not ok:
                    return Fill(False, tx=sig, error=err, route=route)
                if res.get("outputAmountResult"):
                    return Fill(True, sol=int(res["outputAmountResult"]) / 1e9 - order_fees_sol(quote, self.pubkey),
                                tokens=tokens, tx=sig, route=route)
            else:
                ok, sig, detail = await self._execute_rpc(quote)
                if not ok:
                    return Fill(False, tx=sig, error=detail, route=route)
        except Exception as e:  # noqa: BLE001
            return Fill(False, error=f"swap failed: {e}", route=route)
        fill = await self.rpc.tx_fill(sig, self.pubkey, mint)
        if fill is None:
            return Fill(True, sol=int(quote.get("otherAmountThreshold") or quote["outAmount"]) / 1e9, tokens=tokens,
                        tx=sig, route=route + " (estimated from quote)")
        sol, _ = fill
        return Fill(True, sol=sol, tokens=tokens, tx=sig, route=route)

    async def token_holdings(self, mint: str) -> float:
        return (await self.rpc.token_balance(self.pubkey, mint))[0]
