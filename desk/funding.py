"""WHO FUNDED THE CREATOR — on-chain, no paid API (user's rule 2026-11-12).

Serial ruggers launch every token from a FRESH wallet (on 46 bought tokens, 42 creators had no earlier launch), so
the creator's own history says little. The SOL that pays for a fresh wallet has to come from somewhere: the first
transaction of the wallet shows who sent it. This module reads that funder (and the funder's funder, when it is a
fresh wallet too) with plain Solana RPC calls, and the Filter blocks a buy when one of those wallets is linked to a
rug: it is in the rug ledger, or it funded another creator that is.

Exchanges and big services fund thousands of wallets: a funder with `hub_signatures` or more transactions in the
last hour is treated as a hub and never used for a link (it would block every honest token funded from Binance).

Results are cached in the `wallet_funders` table (learning data, never deleted): the ledger's own creators are
looked up in the background (backfill), so a new creator funded by the same wallet as an old rugger is caught.
"""
from __future__ import annotations

import time

SYSTEM_TYPES = ("transfer", "createAccount", "transferWithSeed", "createAccountWithSeed")


async def history(rpc, wallet: str, pages: int = 3) -> tuple[int, dict | None, bool]:
    """(signatures seen, oldest signature info, complete). complete = the wallet's whole history was read."""
    before, n, last = None, 0, None
    for _ in range(max(1, pages)):
        opt: dict = {"limit": 1000}
        if before:
            opt["before"] = before
        sigs = await rpc.call("getSignaturesForAddress", [wallet, opt]) or []
        n += len(sigs)
        if sigs:
            last = sigs[-1]
        if len(sigs) < 1000:
            return n, last, True
        before = sigs[-1]["signature"]
    return n, last, False


def funder_in_tx(tx: dict, wallet: str) -> tuple[str | None, float]:
    """The wallet that sent SOL to `wallet` (or created it) in a parsed transaction: (funder, SOL)."""
    if not tx:
        return None, 0.0
    ins = list(((tx.get("transaction") or {}).get("message") or {}).get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ins += inner.get("instructions") or []
    for i in ins:
        p = i.get("parsed")
        if not isinstance(p, dict) or i.get("program") != "system" or p.get("type") not in SYSTEM_TYPES:
            continue
        info = p.get("info") or {}
        if (info.get("destination") == wallet or info.get("newAccount") == wallet) and info.get("source") \
                and info.get("source") != wallet:
            return info["source"], float(info.get("lamports") or 0) / 1e9
    return None, 0.0


async def find_funder(rpc, wallet: str, pages: int = 3) -> dict:
    """{"funder", "sol", "first_ts", "n", "complete"}: funder None when the history is too long to read (an old,
    busy wallet: not a fresh one) or the first transaction is not a plain SOL transfer."""
    n, first, complete = await history(rpc, wallet, pages)
    out = {"funder": None, "sol": 0.0, "first_ts": (first or {}).get("blockTime"), "n": n, "complete": complete}
    if not complete or not first:
        return out
    tx = await rpc.call("getTransaction", [first["signature"], {"encoding": "jsonParsed",
                                                                 "maxSupportedTransactionVersion": 0}])
    out["funder"], out["sol"] = funder_in_tx(tx, wallet)
    return out


async def is_hub(rpc, wallet: str, hub_signatures: int = 1000, window_s: float = 3600) -> bool:
    """An exchange / service wallet: hub_signatures or more transactions in the last window_s seconds."""
    sigs = await rpc.call("getSignaturesForAddress", [wallet, {"limit": min(1000, hub_signatures)}]) or []
    if len(sigs) < min(1000, hub_signatures):
        return False
    oldest = sigs[-1].get("blockTime") or 0
    return bool(oldest) and time.time() - oldest <= window_s


class Funding:
    """Funder lookups with a DB cache, plus the rug link check used by the Filter."""

    def __init__(self, store, rpc, hops: int = 2, hub_signatures: int = 1000) -> None:
        self.store = store
        self.rpc = rpc
        self.hops = max(1, hops)
        self.hub_signatures = hub_signatures

    async def funder(self, wallet: str) -> dict:
        row = self.store.wallet_funder(wallet)
        if row is not None:
            return row
        info = await find_funder(self.rpc, wallet)
        hub = bool(info["funder"]) and await is_hub(self.rpc, info["funder"], self.hub_signatures)
        row = {"wallet": wallet, "funder": info["funder"], "sol": info["sol"], "first_ts": info["first_ts"],
               "n": info["n"], "complete": info["complete"], "hub": hub}
        self.store.save_wallet_funder(row)
        return row

    async def chain(self, wallet: str) -> list[dict]:
        """Funder rows from the creator up, at most `hops` (stops at a hub, an unknown funder or a loop)."""
        out, seen, w = [], {wallet}, wallet
        for _ in range(self.hops):
            row = await self.funder(w)
            out.append(row)
            f = row.get("funder")
            if not f or row.get("hub") or f in seen:
                break
            seen.add(f)
            w = f
        return out

    def rug_link(self, funder: str) -> str | None:
        """Why this (non-hub) funder is linked to a rug, or None."""
        known = self.store.rugger(funder)
        if known:
            return f"funder {funder[:6]} in rug ledger ({(known['reason'] or '')[:50]})"
        sib = self.store.rugged_siblings(funder)
        if sib:
            return f"funder {funder[:6]} also funded {sib[0]['wallet'][:6]}, in rug ledger ({(sib[0]['reason'] or '')[:40]})"
        return None

    async def check(self, creator: str) -> tuple[str | None, list[dict]]:
        """(reason to block or None, funder chain)."""
        rows = await self.chain(creator)
        for row in rows:
            f = row.get("funder")
            if f and not row.get("hub"):
                why = self.rug_link(f)
                if why:
                    return why, rows
        return None, rows
