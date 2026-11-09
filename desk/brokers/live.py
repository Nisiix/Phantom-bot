"""Live broker: real swaps through Jupiter, signed locally, sent through your RPC.

The private key is read from WALLET_PRIVATE_KEY (.env) and never leaves this
process: Jupiter builds an unsigned transaction, we sign it here, and send it
to the Solana RPC. Fills are measured from on-chain balance changes, not quotes.
"""
from __future__ import annotations

import asyncio
import base64
import json

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ..clients.jupiter import Jupiter
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
    unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    signed = VersionedTransaction(unsigned.message, [kp])
    return base64.b64encode(bytes(signed)).decode()


class LiveBroker(Broker):
    mode = "live"

    def __init__(self, rpc: SolanaRPC, jup: Jupiter, secret: str, max_priority_lamports: int,
                 fee_reserve_sol: float) -> None:
        self.rpc = rpc
        self.jup = jup
        self.kp = load_keypair(secret)
        self.pubkey = str(self.kp.pubkey())
        self.max_priority = max_priority_lamports
        self.reserve = fee_reserve_sol

    async def cash(self) -> float:
        return max(0.0, await self.rpc.sol_balance(self.pubkey) - self.reserve)

    async def _swap(self, quote: dict) -> tuple[bool, str, str]:
        tx_b64 = await self.jup.swap_tx(quote, self.pubkey, self.max_priority)
        sig = await self.rpc.send_raw(sign_swap(tx_b64, self.kp))
        ok, detail = await self.rpc.confirm(sig)
        return ok, sig, detail

    async def _settle(self, read, before: float, timeout: float = 20.0) -> float:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        val = await read()
        while abs(val - before) < 1e-12 and loop.time() < end:
            await asyncio.sleep(1.0)
            val = await read()
        return val

    async def buy(self, mint: str, sol_amount: float, decimals: int, quote: dict) -> Fill:
        sol0 = await self.rpc.sol_balance(self.pubkey)
        tok0, _ = await self.rpc.token_balance(self.pubkey, mint)
        try:
            ok, sig, detail = await self._swap(quote)
        except Exception as e:  # noqa: BLE001
            return Fill(False, error=f"swap failed: {e}")
        if not ok:
            return Fill(False, tx=sig, error=detail)
        tok1 = await self._settle(lambda: self._tok(mint), tok0)
        sol1 = await self.rpc.sol_balance(self.pubkey)
        return Fill(True, sol=sol0 - sol1, tokens=tok1 - tok0, tx=sig)

    async def _tok(self, mint: str) -> float:
        return (await self.rpc.token_balance(self.pubkey, mint))[0]

    async def sell(self, mint: str, tokens: float, decimals: int, quote: dict) -> Fill:
        sol0 = await self.rpc.sol_balance(self.pubkey)
        try:
            ok, sig, detail = await self._swap(quote)
        except Exception as e:  # noqa: BLE001
            return Fill(False, error=f"swap failed: {e}")
        if not ok:
            return Fill(False, tx=sig, error=detail)
        sol1 = await self._settle(lambda: self.rpc.sol_balance(self.pubkey), sol0)
        return Fill(True, sol=sol1 - sol0, tokens=tokens, tx=sig)

    async def token_holdings(self, mint: str) -> float:
        return await self._tok(mint)
