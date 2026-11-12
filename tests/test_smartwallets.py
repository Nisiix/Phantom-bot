"""Smart wallets (user 2026-11-12): early buyers of followed tokens read on-chain, scored on the $200k winners."""
from __future__ import annotations

import asyncio
import time

from desk.config import SmartWalletsCfg
from desk.smartwallets import SmartWallets, parse_trades, real_cap
from desk.store import Store

POOL = "CURVE"


def trade_tx(mint: str, buyer: str, tokens: float, sol: float = 0.5, signer: bool = True, payer: str = "") -> dict:
    """A bonding-curve buy: the curve's balance falls, the buyer's rises; the buyer (or a payer) signs."""
    keys = [{"pubkey": payer or buyer, "signer": True}]
    if payer:
        keys.append({"pubkey": buyer, "signer": False})
    keys.append({"pubkey": POOL, "signer": False})
    b = lambda owner, amt: {"mint": mint, "owner": owner, "uiTokenAmount": {"uiAmountString": str(amt)}}
    return {"meta": {"err": None, "preBalances": [10_000_000_000] * len(keys),
                     "postBalances": [10_000_000_000 - int(sol * 1e9)] + [10_000_000_000] * (len(keys) - 1),
                     "preTokenBalances": [b(POOL, 1_000_000_000)],
                     "postTokenBalances": [b(POOL, 1_000_000_000 - tokens), b(buyer, tokens)]},
            "transaction": {"message": {"accountKeys": keys}}}


class Rpc:
    """mint -> list of (signature, slot, signer of the tx, tx); the first entry is the create transaction."""

    def __init__(self, chains: dict, holdings: dict | None = None) -> None:
        self.chains, self.holdings, self.calls = chains, holdings or {}, 0
        self.txs = {sig: tx for ch in chains.values() for sig, _, _, tx in ch}

    async def call(self, method, params, lane=""):
        self.calls += 1
        if method == "getSignaturesForAddress":
            ch = self.chains.get(params[0], [])
            return [{"signature": s, "slot": slot, "blockTime": 1000 + slot, "err": None}
                    for s, slot, _, _ in reversed(ch)]                       # newest first, like the RPC
        if method == "getTransaction":
            return self.txs.get(params[0])
        if method == "getAccountInfo":
            return {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}}
        if method == "getMultipleAccounts":
            return {"value": [self.holdings.get(a) for a in params[0]]}
        raise AssertionError(method)


def chain(mint: str, creator: str, buyers: list[str], bundle: list[str] = ()) -> list:
    out = [(f"{mint}-create", 10, creator, {"meta": {"err": None}, "transaction": {"message": {
        "accountKeys": [{"pubkey": creator, "signer": True}]}}})]
    for i, w in enumerate(bundle):
        out.append((f"{mint}-b{i}", 10, creator, trade_tx(mint, w, 1000, payer=creator)))
    for i, w in enumerate(buyers):
        out.append((f"{mint}-{i}", 20 + i, w, trade_tx(mint, w, 1000)))
    return out


def cfg(**kw) -> SmartWalletsCfg:
    c = SmartWalletsCfg(rpc_interval_seconds=0, min_wins=2, min_tokens=2, min_hit_rate=0.5, decide_hours=1)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_parse_trades_skips_the_pool_and_flags_non_signers():
    t = parse_trades(trade_tx("M", "W", 500, sol=0.2), "M")
    assert t == [{"wallet": "W", "side": "buy", "tokens": 500.0, "sol": 0.0, "signer": True}] or \
        (t[0]["wallet"] == "W" and t[0]["side"] == "buy" and t[0]["signer"])
    t = parse_trades(trade_tx("M", "FRESH", 500, payer="DEV"), "M")
    assert [(x["wallet"], x["signer"]) for x in t] == [("FRESH", False)]


def test_real_cap_rejects_dust_pools():
    assert real_cap(300_000, 40_000)
    assert not real_cap(72_000_000, 2_000)
    assert not real_cap(300_000, 5_000)


def test_scan_scores_and_watch(tmp_path):
    st = Store(tmp_path / "p.db")
    chains = {
        "W1pump": chain("W1pump", "DEV1", ["SMART", "SNIPER", "LUCKY"], bundle=["B1", "B2"]),
        "W2pump": chain("W2pump", "DEV2", ["SMART", "SNIPER"]),
        "L1pump": chain("L1pump", "DEV3", ["SNIPER"]),
        "L2pump": chain("L2pump", "DEV4", ["SNIPER"]),
        "L3pump": chain("L3pump", "DEV5", ["SNIPER", "SMART"]),
    }
    rpc = Rpc(chains)
    sw = SmartWallets(st, rpc, None, cfg())
    for m in chains:
        asyncio.run(sw.scan(m, m[:2]))
    old = time.time() - 7200
    st.db.execute("UPDATE sw_tokens SET create_ts=?", (old,))
    st.db.execute("UPDATE sw_tokens SET won=1, peak_mcap=300000 WHERE mint IN ('W1pump','W2pump')")
    rows = {r["wallet"]: r for r in st.q("SELECT * FROM sw_trades WHERE mint='W1pump'")}
    assert rows["B1"]["bundle"] == 1 and rows["B1"]["signer"] == 0
    assert rows["SMART"]["signer"] == 1 and rows["SMART"]["bundle"] == 0
    smart = sw.rescore()
    # SMART: 2 wins out of 3 tokens; SNIPER: 2 of 5 (40% < 50%); LUCKY: 1 win only; bundle wallets never count
    assert set(smart) == {"SMART"} and smart["SMART"]["wins"] == 2 and smart["SMART"]["tokens"] == 3

    from solders.pubkey import Pubkey

    from desk.devwatch import ata
    real, mint = str(Pubkey.new_unique()), str(Pubkey.new_unique())     # ATAs need real addresses
    sw.smart = {real: {**smart["SMART"], "wallet": real}}
    acc = {"data": {"parsed": {"info": {"tokenAmount": {"uiAmountString": "1234"}}}}}
    rpc.holdings = {ata(real, mint): acc}
    hits = asyncio.run(sw.watch(mint, "NEW", "analyst"))
    assert hits == [real]
    assert asyncio.run(sw.watch(mint, "NEW", "analyst")) == []           # one hit per wallet and token
    row = st.q("SELECT * FROM sw_hits")[0]
    assert row["wallet"] == real and row["held_at_start"] == 1 and row["wins"] == 2


def test_undecided_tokens_do_not_count_as_losses(tmp_path):
    st = Store(tmp_path / "p.db")
    chains = {m: chain(m, "D" + m, ["A"]) for m in ("W1pump", "W2pump", "NEWpump")}
    sw = SmartWallets(st, Rpc(chains), None, cfg(min_hit_rate=0.9))
    for m in chains:
        asyncio.run(sw.scan(m))
    st.db.execute("UPDATE sw_tokens SET create_ts=? WHERE mint!='NEWpump'", (time.time() - 7200,))
    st.db.execute("UPDATE sw_tokens SET create_ts=? WHERE mint='NEWpump'", (time.time(),))
    st.db.execute("UPDATE sw_tokens SET won=1 WHERE mint IN ('W1pump','W2pump')")
    assert "A" in sw.rescore()                      # 2/2: the 1-minute-old token is not a loss yet


def test_long_history_is_not_read(tmp_path):
    st = Store(tmp_path / "p.db")

    class Busy(Rpc):
        async def call(self, method, params, lane=""):
            self.calls += 1
            assert method == "getSignaturesForAddress"
            return [{"signature": f"s{self.calls}-{i}", "slot": 1, "blockTime": 1, "err": None} for i in range(1000)]

    sw = SmartWallets(st, Busy({}), None, cfg(max_sig_pages=2))
    row = asyncio.run(sw.scan("BIGpump"))
    assert row["complete"] == 0 and sw.scanned("BIGpump")


def test_bot_peak_ignores_dust_pool_caps(tmp_path):
    st = Store(tmp_path / "p.db")
    ins = "INSERT INTO decisions(ts,stage,outcome,mint,symbol,reason,data) VALUES(?,?,?,?,?,?,?)"
    st.db.execute(ins, (1, "filter", "reject", "Xpump", "X", "", '{"checks": ["FAIL liquidity: 2000 vs 20000.0", '
                                                                 '"PASS market_cap: 1 vs 1 (market cap $72,000,000)"]}'))
    st.db.execute(ins, (2, "filter", "reject", "Xpump", "X", "", '{"checks": ["PASS liquidity: 40000 vs 20000.0", '
                                                                 '"PASS market_cap: 1 vs 1 (market cap $250,000)"]}'))
    sw = SmartWallets(st, Rpc({}), None, cfg())
    assert sw.bot_peak("Xpump") == (250_000.0, 2)
    assert sw.bot_peaks()["Xpump"][0] == 250_000.0


def test_whale_checker_agent_orders_and_watches():
    from types import SimpleNamespace as NS

    from desk.agents.whale import WhaleChecker
    from desk.hub import AGENTS, Hub

    assert "Whale checker" in AGENTS
    sh = NS(active={"a": NS(mint="Fpump", symbol="F", stage="filter", start=2.0),
                    "b": NS(mint="Apump", symbol="A", stage="analyst", start=1.0),
                    "c": NS(mint="Npump", symbol="N", stage="grad45", start=3.0)})
    desk = NS(cfg=NS(smart_wallets=SmartWalletsCfg()), hub=Hub(), shadow=sh,
              portfolio=NS(positions={"Ppump": NS(symbol="P")}))
    wc = WhaleChecker(desk)
    assert [m for m, _ in wc.followed()] == ["Ppump", "Apump", "Npump", "Fpump"]
    assert [(m, st) for m, _, st in wc.watched()] == [("Ppump", "position"), ("Apump", "analyst")]
