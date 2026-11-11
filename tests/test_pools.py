"""Buy in the observed pool, in the currency it trades against (user rule 2026-11-12, desk/pools.py)."""
from __future__ import annotations

import asyncio

from desk.agents.executor import Executor
from desk.agents.risk import Risk
from desk.brokers.live import LiveBroker
from desk.brokers.paper import PaperBroker
from desk.clients.dexscreener import snapshot_from_pairs
from desk.pools import SOL_MINT, pin_labels, route_check, route_text
from tests.test_rules import make_desk, signed_verdict, snap

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def pair(addr, dex, quote, liq, sym="USDC"):
    return {"chainId": "solana", "pairAddress": addr, "dexId": dex, "priceUsd": "0.001", "priceNative": "0.00001",
            "baseToken": {"address": "M", "symbol": "TST"},
            "quoteToken": {"address": quote, "symbol": "SOL" if quote == SOL_MINT else sym},
            "liquidity": {"usd": liq}}


def leg(amm, i, o, label="Pump.fun Amm"):
    return {"swapInfo": {"ammKey": amm, "label": label, "inputMint": i, "outputMint": o}, "percent": 100}


def test_dust_sol_pool_is_not_the_observed_pool():
    s = snapshot_from_pairs("M", [pair("SOLDUST", "raydium", SOL_MINT, 300), pair("DEEP", "meteora", USDC, 90_000)])
    assert s.pair_address == "DEEP" and s.quote_mint == USDC and s.quote_symbol == "USDC"
    assert s.pair_liquidity_usd == 90_000 and len(s.pools) == 2 and s.price_native == 0
    s = snapshot_from_pairs("M", [pair("SOL", "pumpswap", SOL_MINT, 60_000), pair("U", "meteora", USDC, 90_000)])
    assert s.pair_address == "SOL" and s.quote_mint == SOL_MINT          # comparable SOL pool still preferred


def test_route_check_and_text():
    direct = {"routePlan": [leg("P", SOL_MINT, "M")]}
    assert route_check(direct, "M", "P")[0]
    other = {"routePlan": [leg("X", SOL_MINT, "M", "Raydium")]}
    ok, why = route_check(other, "M", "P")
    assert not ok and "another pool" in why
    hop = {"routePlan": [leg("W", SOL_MINT, USDC, "Whirlpool"), leg("P", USDC, "M", "Meteora DLMM")]}
    assert route_check(hop, "M", "P")[0]                    # SOL -> USDC anywhere, USDC -> token in our pool
    assert route_text(hop, {"M": "TST", USDC: "USDC"}).startswith("SOL -> USDC (Whirlpool W…) -> TST (Meteora DLMM")
    assert not route_check({"router": "jupiterz"}, "M", "P")[0]          # RFQ: no pool disclosed


def test_pin_labels_add_conversion_venues_only_for_non_sol_pools():
    pools = [{"address": "P", "dex": "meteora", "quote_mint": USDC},
             {"address": "Q", "dex": "orca", "quote_mint": SOL_MINT}]
    labels = pin_labels(pools[0], pools)
    assert "Meteora DLMM" in labels and "Raydium CLMM" in labels and "Whirlpool" not in labels   # token has an orca pool
    assert pin_labels({"address": "S", "dex": "pumpswap", "quote_mint": SOL_MINT}, pools) == ["Pump.fun Amm"]
    assert pin_labels({"address": "S", "dex": "weirddex", "quote_mint": SOL_MINT}, pools) == []


def _setup(tmp_path, pools_now, routes):
    desk = make_desk(tmp_path)
    asked = []

    class FakeJup:
        async def quote(self, i, o, amount, bps, lane="", dexes=None):
            asked.append(dexes)
            if i == SOL_MINT:
                amm = routes["pinned" if dexes else "default"]
                return {"inAmount": str(amount), "outAmount": str(int(amount / 1e9 / 0.00001 * 1e6)),
                        "routePlan": [leg(amm, SOL_MINT, "M")]}
            return {"outAmount": str(int(int(amount) * 0.00001 / 1e6 * 1e9 * 0.98))}

    class FakeDex:
        async def snapshot(self, mint, fresh=True):
            return snap(mint=mint, pools=pools_now)

    desk.jup, desk.dex = FakeJup(), FakeDex()
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6
    v.clearance.report.market = snap(pools=[{"address": "P", "dex": "pumpswap", "quote_mint": SOL_MINT,
                                             "quote_symbol": "SOL", "liquidity_usd": 80_000}])
    return desk, ex, v, asked


POOL = {"address": "P", "dex": "pumpswap", "quote_mint": SOL_MINT, "quote_symbol": "SOL", "liquidity_usd": 80_000}
DUST = {"address": "X", "dex": "raydium", "quote_mint": SOL_MINT, "quote_symbol": "SOL", "liquidity_usd": 50}


def test_buy_is_forced_back_on_the_observed_pool(tmp_path):
    desk, ex, v, asked = _setup(tmp_path, [POOL, DUST], {"default": "X", "pinned": "P"})
    asyncio.run(ex.handle_verdict(v))
    pos = desk.portfolio.positions["M"]
    assert ["Pump.fun Amm"] in asked
    assert "pool pumpswap P… vs SOL" in pos.entry_reason and "forced on the observed pool" in pos.entry_reason


def test_no_buy_when_jupiter_cannot_use_the_observed_pool(tmp_path):
    desk, ex, v, _ = _setup(tmp_path, [POOL, DUST], {"default": "X", "pinned": "X"})
    asyncio.run(ex.handle_verdict(v))
    assert "M" not in desk.portfolio.positions
    assert "another pool" in desk.store.db.execute("SELECT reason FROM decisions ORDER BY rowid DESC").fetchone()[0]


def test_no_buy_when_observed_pool_drained(tmp_path):
    drained = {**POOL, "liquidity_usd": 30_000}
    desk, ex, v, _ = _setup(tmp_path, [drained, DUST], {"default": "P", "pinned": "P"})
    asyncio.run(ex.handle_verdict(v))
    assert "M" not in desk.portfolio.positions
    assert "fell from $80,000" in desk.store.db.execute(
        "SELECT reason FROM decisions ORDER BY rowid DESC").fetchone()[0]


def test_live_pinned_quote_goes_through_rpc_path():
    from solders.keypair import Keypair
    me = Keypair()
    sent = []

    class FakeJup:
        async def swap_tx(self, quote, pubkey, prio):
            sent.append("swap_tx")
            return "TX", 100

        async def execute(self, *a):
            raise AssertionError("a /swap/v1 quote must not go to /swap/v2/execute")

    class FakeRPC:
        async def send_and_confirm(self, tx, lvbh):
            return True, "SIG", ""

        async def tx_fill(self, sig, owner, mint):
            return -0.5, 1500.0

    b = LiveBroker(FakeRPC(), FakeJup(), str(me), 200_000, 0.05)
    import desk.brokers.live as live
    orig, live.sign_swap = live.sign_swap, lambda tx, kp: tx
    try:
        fill = asyncio.run(b.buy("M", 0.5, 6, {"inAmount": "500000000", "outAmount": "1500000000",
                                                "routePlan": [leg("P", SOL_MINT, "M")]}))
    finally:
        live.sign_swap = orig
    assert fill.ok and sent == ["swap_tx"] and fill.route == "rpc metis" and fill.tokens == 1500.0
