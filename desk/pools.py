"""Which pool a buy goes through, and in which currency (user rule 2026-11-12).

A token can have several pools: PumpSwap against SOL, a Meteora or Raydium pool against USDC, dust copies with
no liquidity. The Filter and the Analyst judge the numbers of ONE pool (MarketSnapshot.pair_address). The buy
must go through that same pool:

  * pool quoted in SOL  -> SOL -> token in that pool;
  * pool quoted in another currency (USDC, USDT, ...) -> SOL -> that currency first, then that currency -> token
    in that pool. Jupiter does both swaps in ONE transaction (multi-hop route), so if the second swap fails the
    first is undone too: no USDC is ever left stranded in the wallet, and only one network fee is paid. The
    quote's amounts already include both swaps' pool fees and price impact, so paper fills carry them too.
  * selling is the same in reverse: token -> currency -> SOL, again in one transaction, so the balance always
    comes back in SOL (every sell quote asks Jupiter for SOL as output).

Jupiter picks its own route. route_check() reads the route it returned (routePlan: every leg's pool = ammKey) and
tells whether the token leg goes through the observed pool. When it doesn't, the Executor asks again restricted
to that pool's DEX (pin_labels) and checks again; if Jupiter still can't route through the pool, no buy.
"""
from __future__ import annotations

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
# Currencies a token's main pool may trade against (user rule 2026-11-12, after BERNIE). A pool quoted in another
# memecoin (BERNIE/SPCX) carries that coin's risk too, and its few dust SOL copies show made-up prices.
DEFAULT_QUOTES = [SOL_MINT, USDC_MINT, USDT_MINT]


def quote_ok(quote_mint: str, allowed: list[str]) -> bool:
    """True when the pool's quote currency is allowed (unknown quote = not judged)."""
    return not quote_mint or not allowed or quote_mint in allowed

# DexScreener dexId -> Jupiter route labels (GET /swap/v1/program-id-to-label, checked 2026-11-12).
DEX_LABELS = {
    "pumpswap": ["Pump.fun Amm"],
    "pumpfun": ["Pump.fun"],
    "raydium": ["Raydium", "Raydium CLMM", "Raydium CP"],
    "launchlab": ["Raydium Launchlab"],
    "meteora": ["Meteora", "Meteora DLMM", "Meteora DAMM v2", "Dynamic Bonding Curve"],
    "meteoradbc": ["Dynamic Bonding Curve"],
    "orca": ["Whirlpool", "Orca V1", "Orca V2"],
    "boop": ["Boop.fun"],
    "moonit": ["Moonit"],
    "heaven": ["Heaven"],
    "saros": ["Saros", "Saros DLMM"],
}
# Deep SOL/USDC/USDT venues for the conversion leg when the token's pool doesn't trade against SOL.
CONVERSION_LABELS = ["Whirlpool", "Raydium CLMM", "Meteora DLMM", "Raydium"]


def legs(quote: dict | None) -> list[dict]:
    return [(r or {}).get("swapInfo") or {} for r in (quote or {}).get("routePlan") or []]


def route_check(quote: dict | None, mint: str, pool: str) -> tuple[bool, str]:
    """True when every leg that buys or sells `mint` goes through `pool`."""
    tok = [x for x in legs(quote) if mint in (x.get("inputMint"), x.get("outputMint"))]
    if not tok:
        return False, "Jupiter's route shows no pool for the token (RFQ / route not disclosed)"
    other = sorted({x.get("ammKey", "?") for x in tok if x.get("ammKey") != pool})
    if other:
        labels = sorted({x.get("label", "?") for x in tok if x.get("ammKey") != pool})
        return False, (f"Jupiter routed the token through another pool ({', '.join(a[:6] + '…' for a in other)} "
                       f"on {', '.join(labels)}), not the observed {pool[:6]}…")
    return True, "ok"


def pin_labels(pool: dict, pools: list[dict]) -> list[str]:
    """Jupiter labels that keep the route on the observed pool's DEX (plus deep venues for the SOL -> currency leg
    when the pool doesn't trade against SOL, skipping venues where the token has another pool)."""
    own = DEX_LABELS.get((pool.get("dex") or "").lower(), [])
    if not own:
        return []
    out = list(own)
    if pool.get("quote_mint") and pool.get("quote_mint") != SOL_MINT:
        taken = {lab for p in pools if p.get("address") != pool.get("address")
                 for lab in DEX_LABELS.get((p.get("dex") or "").lower(), [])}
        out += [lab for lab in CONVERSION_LABELS if lab not in out and lab not in taken]
    return out


def route_text(quote: dict | None, symbols: dict[str, str]) -> str:
    """'SOL -> USDC (Whirlpool) -> TST (Meteora DLMM 31p1hp…)' for the trade log."""
    ls = legs(quote)
    if not ls:
        return ""
    known = {SOL_MINT: "SOL", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
             "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
    sym = lambda m: symbols.get(m) or known.get(m) or (m or "?")[:4]  # noqa: E731
    parts, last = [sym(ls[0].get("inputMint"))], None
    for x in ls:
        if x.get("outputMint") == last:
            continue                              # split route: same hop on several pools, show once
        parts.append(f"{sym(x.get('outputMint'))} ({x.get('label', '?')} {(x.get('ammKey') or '?')[:6]}…)")
        last = x.get("outputMint")
    return " -> ".join(parts)
