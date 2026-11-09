"""On-chain LP check: who can actually pull the liquidity, read straight from Solana (no RugCheck numbers).

Solana tokens don't have their own contract code (every token runs the standard SPL Token / Token-2022
program), so the rug risk is in who controls the liquidity. RugCheck is still used to LIST the pools of a
token; the numbers below come from the chain.

Classic AMM pools (PumpSwap, Raydium AMM / CPMM, ...) have an LP token:
  locked % = share of the CURRENT LP supply that no wallet can redeem. Burned LP is gone from the supply
  (supply 0 = all burned); LP held by a program address (PDA: lockers like Raydium Burn & Earn, the pool
  itself) counts as locked; LP held by an ordinary wallet is pullable. Holders below the 20 largest are
  assumed pullable (conservative).

Concentrated pools (Meteora DLMM / DAMM v2, Raydium CLMM, Orca whirlpools) have no LP token, every
position is its own account with an owner. We list the pool's positions and check the owners against the
dev, the dev's funder, the rug ledger and the top holders:
  "dev"        a suspicious wallet provides liquidity          -> the Filter rejects the token
  "clean"      every position owner checked, none suspicious   -> third-party liquidity, not capped
  "unverified" too many NFT positions / RPC refused             -> falls back to the 50% cap
DLMM positions carry their owner directly (one RPC call). DAMM v2 / CLMM / Orca positions are NFTs, so each
owner costs an extra lookup: those are checked only with a private RPC (SOLANA_RPC_URL), where it is cheap.
"""
from __future__ import annotations

import asyncio
import base64
import time

from solders.pubkey import Pubkey

SYSTEM_PROGRAM = "11111111111111111111111111111111"
BURN_ADDRESSES = {"1nc1nerator11111111111111111111111111111111", SYSTEM_PROGRAM}

# program id, account discriminator, offset of the pool address, offset of the owner / position NFT mint
POSITION_LAYOUTS = {
    "dlmm": ("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo", "75b0d4c7f5b485b6", 8, 40, "owner"),
    "damm_v2": ("cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG", "aabc8fe47a40f7d0", 8, 40, "nft"),
    "clmm": ("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", "466f967ee60f1975", 41, 9, "nft"),
    "whirlpool": ("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", "aabc8fe47a40f7d0", 8, 40, "nft"),
}
MAX_NFT_POSITIONS = 25
CACHE_TTL = 300.0

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def concentrated_kind(market_type: str) -> str | None:
    mt = market_type.lower().replace("dammv2", "damm_v2")
    if "dlmm" in mt:
        return "dlmm"
    if "damm_v2" in mt:
        return "damm_v2"
    if "clmm" in mt:
        return "clmm"
    if "whirlpool" in mt or "orca" in mt:
        return "whirlpool"
    return None


def is_program_controlled(owner: str) -> bool:
    """A PDA (off the ed25519 curve) has no private key: only its program can move what it holds."""
    try:
        return not Pubkey.from_string(owner).is_on_curve()
    except ValueError:
        return False


class OnchainLP:
    def __init__(self, rpc, private_rpc: bool) -> None:
        self.rpc = rpc
        self.private_rpc = private_rpc
        self._cache: dict[str, tuple[float, dict]] = {}

    def _cached(self, key: str):
        hit = self._cache.get(key)
        return hit[1] if hit and time.time() - hit[0] < CACHE_TTL else None

    def _store(self, key: str, val: dict) -> dict:
        if len(self._cache) > 3000:
            self._cache.clear()
        self._cache[key] = (time.time(), val)
        return val

    # ── classic pools ──
    async def classic_locked_pct(self, lp_mint: str) -> dict:
        hit = self._cached("lp:" + lp_mint)
        if hit is not None:
            return hit
        supply = int(((await self.rpc.call("getTokenSupply", [lp_mint])) or {}).get("value", {}).get("amount") or 0)
        if supply <= 0:
            return self._store("lp:" + lp_mint, {"locked_pct": 100.0, "detail": "LP fully burned"})
        largest = ((await self.rpc.call("getTokenLargestAccounts", [lp_mint, {"commitment": "confirmed"}]))
                   or {}).get("value") or []
        accounts = [a["address"] for a in largest]
        owners = await self._token_account_owners(accounts)
        locked = 0
        for a in largest:
            owner = owners.get(a["address"])
            if owner and (owner in BURN_ADDRESSES or is_program_controlled(owner)):
                locked += int(a.get("amount") or 0)
        pct = min(100.0, locked / supply * 100)
        return self._store("lp:" + lp_mint, {"locked_pct": pct,
                                             "detail": f"{pct:.0f}% of LP supply burned/program-held"})

    async def _token_account_owners(self, accounts: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in range(0, len(accounts), 100):
            chunk = accounts[i:i + 100]
            res = await self.rpc.call("getMultipleAccounts", [chunk, {"encoding": "jsonParsed",
                                                                      "commitment": "confirmed"}])
            for addr, acc in zip(chunk, (res or {}).get("value") or []):
                info = (((acc or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                if info.get("owner"):
                    out[addr] = info["owner"]
        return out

    # ── concentrated pools ──
    async def position_owners(self, kind: str, pool: str) -> tuple[list[str], bool]:
        """(owners, complete). complete=False when not every position could be checked."""
        hit = self._cached(f"pos:{pool}")
        if hit is not None:
            return hit["owners"], hit["complete"]
        prog, disc, pool_off, key_off, key_kind = POSITION_LAYOUTS[kind]
        if key_kind == "nft" and not self.private_rpc:
            return [], False
        res = await self.rpc.call("getProgramAccounts", [prog, {
            "encoding": "base64", "dataSlice": {"offset": key_off, "length": 32},
            "filters": [{"memcmp": {"offset": 0, "bytes": b58encode(bytes.fromhex(disc))}},
                        {"memcmp": {"offset": pool_off, "bytes": pool}}]}]) or []
        keys = [str(Pubkey.from_bytes(base64.b64decode(a["account"]["data"][0])[:32])) for a in res]
        complete = True
        if key_kind == "owner":
            owners = keys
        else:
            if len(keys) > MAX_NFT_POSITIONS:
                keys, complete = keys[:MAX_NFT_POSITIONS], False
            holders = await asyncio.gather(*(self.rpc.call("getTokenLargestAccounts", [m]) for m in keys),
                                           return_exceptions=True)
            accts = []
            for h in holders:
                vals = [] if isinstance(h, BaseException) else ((h or {}).get("value") or [])
                held = [v["address"] for v in vals if int(v.get("amount") or 0) > 0]
                if held:
                    accts.append(held[0])
                else:
                    complete = False
            owners = list((await self._token_account_owners(accts)).values())
        owners = list(dict.fromkeys(owners))
        self._store(f"pos:{pool}", {"owners": owners, "complete": complete})
        return owners, complete

    # ── one token ──
    async def analyze(self, report: dict, suspicious: dict[str, str], rugger=None) -> dict[str, dict]:
        """pool pubkey -> on-chain verdict. suspicious: wallet -> why (dev, funder, top holder);
        rugger(wallet) -> True for wallets in our rug ledger."""
        out: dict[str, dict] = {}

        async def one(m: dict) -> None:
            pk = m.get("pubkey") or ""
            mt = (m.get("marketType") or "").lower()
            lp = m.get("lp") or {}
            if not pk or float(lp.get("quoteUSD") or 0) + float(lp.get("baseUSD") or 0) <= 0:
                return
            if mt in ("pump_fun", "pumpfun", "raydium_launchlab", "launchlab", "meteora_dbc", "letsbonk"):
                return                       # bonding curve: program-held by design
            kind = concentrated_kind(mt)
            try:
                if kind:
                    owners, complete = await self.position_owners(kind, pk)
                    bad = [f"{suspicious[o]} {o[:6]}" for o in owners if o in suspicious]
                    if rugger is not None:
                        bad += [f"known rugger {o[:6]}" for o in owners if o not in suspicious and rugger(o)]
                    if bad:
                        out[pk] = {"status": "dev", "detail": f"{mt} liquidity from " + ", ".join(bad[:3])}
                    elif complete:
                        out[pk] = {"status": "clean", "detail": f"{mt}: {len(owners)} third-party LPs"}
                    else:
                        out[pk] = {"status": "unverified", "detail": f"{mt}: positions not checked"}
                else:
                    lp_mint = m.get("mintLP") or lp.get("lpMint")
                    if lp_mint and lp_mint != SYSTEM_PROGRAM:
                        out[pk] = await self.classic_locked_pct(lp_mint)
            except Exception as e:  # noqa: BLE001 - no on-chain answer: the Filter falls back to RugCheck
                out[pk] = {"status": "unverified", "detail": f"{mt}: RPC {e.__class__.__name__}"}

        await asyncio.gather(*(one(m) for m in report.get("markets") or []))
        return out
