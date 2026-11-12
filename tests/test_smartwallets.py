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


# ── report to the Analyst (user 2026-11-12) ──
def test_smart_wallets_weigh_51_percent_only_with_sufficient_data():
    from desk.agents.analyst import score_token
    from desk.controller import BASE_WEIGHTS, analyst_score
    from tests.test_rules import snap
    web = {"url": "https://example.org", "score": 0.6, "legit": 0.6, "tasty": 0.5, "flags": []}
    rug = {"top10_pct": 12, "holders_total": 900, "insider_pct": 3, "risks": []}
    plain, b0, _ = score_token(snap(), rug, web)
    ignored, _, why = score_token(snap(), {**rug, "whales": {"ready": False, "why": "not enough data yet"}}, web)
    assert ignored == plain and any("ignored" in w for w in why)
    none_held, b1, _ = score_token(snap(), {**rug, "whales": {"ready": True, "holders": 0}}, web)
    assert none_held == plain and "whales" not in b1                    # no smart holder: the usual rules
    info: dict = {}
    one, b2, why = score_token(snap(), {**rug, "whales": {"ready": True, "holders": 1, "wallets": ["Ab12cd"],
                                                          "entry_sol": 2e-7}}, web, None, info)
    assert b2["whales"] == 1.0 and abs(one - (0.51 + 0.49 * plain)) < 1e-3 and one >= 0.6
    assert info["whale_free_score"] == round(plain, 4) and info["whale_entry_sol"] == 2e-7
    f = {**{f"c_{k}": v for k, v in b2.items()}, **info}
    assert abs(analyst_score(f, BASE_WEIGHTS) - one) < 0.01           # the Controller re-scores it the same way


def test_readiness_and_report(tmp_path):
    st = Store(tmp_path / "p.db")
    chains = {m: chain(m, "D" + m, ["A"]) for m in ("W1pump", "W2pump", "L1pump")}
    rpc = Rpc(chains)
    sw = SmartWallets(st, rpc, None, cfg(ready_min_decided_tokens=3, ready_min_winners=2,
                                         ready_min_smart_wallets=1, min_hit_rate=0.5,
                                         ready_min_validation_tokens=0, ready_min_lift=0, entry_lookup_seconds=2))
    rep = asyncio.run(sw.report("Xpump"))
    assert rep["ready"] is False and "0/3 tokens decided" in rep["why"]
    for m in chains:
        asyncio.run(sw.scan(m))
    st.db.execute("UPDATE sw_tokens SET create_ts=?", (time.time() - 7200,))
    st.db.execute("UPDATE sw_tokens SET won=1 WHERE mint IN ('W1pump','W2pump')")
    sw.rescore()
    sw._ready = {}                                                     # drop the 5-minute cache
    assert sw.readiness()["ready"]
    from solders.pubkey import Pubkey

    from desk.devwatch import ata
    real, mint = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    sw.smart = {real: {**sw.smart["A"], "wallet": real}}
    rpc.holdings = {ata(real, mint): {"data": {"parsed": {"info": {"tokenAmount": {"uiAmountString": "5"}}}}}}
    rep = asyncio.run(sw.report(mint, "TOK"))
    assert rep["ready"] and rep["holders"] == 1 and rep["wallets"] == [real[:6]]
    assert st.q("SELECT stage FROM sw_hits")[0]["stage"] == "analyst"


# ── devil's-advocate safeguards (2026-11-12) ──
def _decided(st, won: dict, creators: dict | None = None):
    st.db.execute("UPDATE sw_tokens SET create_ts=?", (time.time() - 7200,))
    for m, w in won.items():
        st.db.execute("UPDATE sw_tokens SET won=? WHERE mint=?", (int(w), m))
    for m, cr in (creators or {}).items():
        st.db.execute("UPDATE sw_tokens SET creator=? WHERE mint=?", (cr, m))


def test_dust_buys_and_one_win_per_operator(tmp_path):
    st = Store(tmp_path / "p.db")
    chains = {m: chain(m, "D" + m, ["A", "B"]) for m in ("W1pump", "W2pump", "W3pump")}
    rpc = Rpc(chains)
    for m in chains:                       # B buys dust (0.01 SOL) everywhere
        for sig, _, _, tx in chains[m]:
            if tx["transaction"]["message"]["accountKeys"][0]["pubkey"] == "B":
                tx["meta"]["postBalances"][0] = tx["meta"]["preBalances"][0] - int(0.01e9)
    sw = SmartWallets(st, rpc, None, cfg(min_wins=2, min_tokens=2, min_hit_rate=0.5))
    for m in chains:
        asyncio.run(sw.scan(m))
    _decided(st, {m: 1 for m in chains})
    assert set(sw.rescore()) == {"A"}                                   # B's dust buys are not scored
    # the same operator (one funder behind 3 fresh creators) relaunching = one win, not three
    for m in chains:
        st.save_wallet_funder({"wallet": "D" + m, "funder": "BOSS", "complete": True, "hub": False})
    assert sw.rescore() == {}


def test_no_self_proof_and_insiders_sellers_operators(tmp_path):
    from solders.pubkey import Pubkey

    from desk.devwatch import ata
    st = Store(tmp_path / "p.db")
    S1, S2, S3, INS, mint = (str(Pubkey.new_unique()) for _ in range(5))
    chains = {"W1pump": chain("W1pump", "D1", [S1, S2, S3, INS]), "W2pump": chain("W2pump", "D2", [S1, S2, S3, INS]),
              mint: chain(mint, "D3", [S1])}
    rpc = Rpc(chains)
    sw = SmartWallets(st, rpc, None, cfg(min_wins=2, min_tokens=2, min_hit_rate=0.5, ready_min_decided_tokens=1,
                                         ready_min_winners=1, ready_min_smart_wallets=1,
                                         ready_min_validation_tokens=0, ready_min_lift=0))
    for m in chains:
        asyncio.run(sw.scan(m))
    _decided(st, {"W1pump": 1, "W2pump": 1, mint: 1})
    sw.rescore()
    assert {S1, S2, S3, INS} <= set(sw.smart)
    assert sw.smart_for(S1, mint) is not None                         # 2 other winners: still smart without it
    sw.c.min_wins = 3
    sw.rescore()
    assert S1 in sw.smart and sw.smart_for(S1, mint) is None          # smart only thanks to this very token
    sw.c.min_wins = 2
    sw.rescore()
    # S2 and S3 are one operator (same funder); INS is funded by the creator's funder
    st.save_wallet_funder({"wallet": S2, "funder": "OP", "complete": True, "hub": False})
    st.save_wallet_funder({"wallet": S3, "funder": "OP", "complete": True, "hub": False})
    st.save_wallet_funder({"wallet": INS, "funder": "CREATORFUNDER", "complete": True, "hub": False})
    acc = lambda n: {"data": {"parsed": {"info": {"tokenAmount": {"uiAmountString": str(n)}}}}}   # noqa: E731
    new = str(Pubkey.new_unique())
    rpc.holdings = {ata(w, new): acc(100) for w in (S1, S2, S3, INS)}
    rep = asyncio.run(sw.report(new, "NEW", creator="DEV", creator_funder="CREATORFUNDER"))
    assert rep["holders"] == 2 and rep["skipped"] == {"insider": 1, "same_operator": 1}
    sw._hcache.clear()
    rpc.holdings[ata(S1, new)] = acc(30)                              # S1 sold 70% of what it held
    rep = asyncio.run(sw.report(new, "NEW", creator="DEV", creator_funder="CREATORFUNDER"))
    assert rep["holders"] == 1 and rep["skipped"]["selling"] == 1


def test_validation_out_of_sample(tmp_path):
    st = Store(tmp_path / "p.db")
    c = cfg(min_wins=2, min_tokens=2, min_hit_rate=0.5)
    sw = SmartWallets(st, Rpc({}), None, c)
    now = time.time()
    # 20 decided tokens over time; GOOD buys early in the winners, the rest is noise
    ins_t = "INSERT INTO sw_tokens(mint,creator,create_ts,complete,won,scanned_ts) VALUES(?,?,?,?,?,?)"
    ins_b = "INSERT INTO sw_trades(mint,wallet,sig,ts,side,sol,signer,bundle,creator) VALUES(?,?,?,?,?,?,?,?,?)"
    for i in range(20):
        won = int(i % 4 == 0)
        m = f"T{i}pump"
        st.db.execute(ins_t, (m, f"DEV{i}", now - 3 * 86400 + i * 3600, 1, won, now))
        st.db.execute(ins_b, (m, "NOISE", f"s{i}n", now, "buy", 0.5, 1, 0, 0))
        if won:
            st.db.execute(ins_b, (m, "GOOD", f"s{i}g", now, "buy", 0.5, 1, 0, 0))
    v = sw.validate(now)
    assert v["test_tokens"] == 6 and v["n"] == 1 and v["wins"] == 1
    assert abs(v["base"] - 1 / 6) < 1e-9 and abs(v["lift"] - 6.0) < 1e-9  # picked on the old 14, right on the new 6
    sw._validation = v
    c.ready_min_validation_tokens, c.ready_min_lift = 3, 1.2
    sw.smart = {"GOOD": {}}
    c.ready_min_decided_tokens = c.ready_min_winners = c.ready_min_smart_wallets = 1
    sw._ready = {}
    r = sw.readiness(now)
    assert not r["ready"] and "out-of-sample check on 1/3" in r["why"]


def test_rescore_async_reads_in_a_thread_same_result(tmp_path):
    st = Store(tmp_path / "p.db")
    chains = {m: chain(m, "D" + m, ["A", "B"]) for m in ("W1pump", "W2pump", "L1pump")}
    sw = SmartWallets(st, Rpc(chains), None, cfg(min_wins=2, min_tokens=2, min_hit_rate=0.5))
    for m in chains:
        asyncio.run(sw.scan(m))
    _decided(st, {"W1pump": 1, "W2pump": 1, "L1pump": 0})
    sync = dict(sw.rescore())
    sw.smart = {}
    assert set(asyncio.run(sw.rescore_async())) == set(sync) == {"A", "B"}
    assert st.q("SELECT COUNT(*) AS n FROM sw_smart")[0]["n"] == 2


def test_win_line_is_100k_and_old_verdicts_follow_it(tmp_path):
    assert SmartWalletsCfg().win_mcap_usd == 100_000
    st = Store(tmp_path / "p.db")
    sw = SmartWallets(st, Rpc({}), None, cfg(win_mcap_usd=200_000))
    st.db.execute("INSERT INTO sw_tokens(mint,complete,peak_mcap,won) VALUES('Mpump',1,150000,0)")
    SmartWallets(st, Rpc({}), None, cfg(win_mcap_usd=100_000))        # restart with the new line
    assert st.q("SELECT won FROM sw_tokens")[0]["won"] == 1
    assert sw is not None


# ── buying a smart-wallet token (user 2026-11-12, desk/whale_entry.py) ──
def test_whale_entry_decide():
    from desk.whale_entry import decide
    c = SmartWalletsCfg()
    assert decide(10, 1.25, 1.0, 2.0, 1.0, [], c)[0] == "buy"                      # +25% <= +30%
    st, why = decide(10, 1.5, 1.0, 2.0, 1.0, [], c)
    assert st == "wait" and "+50%" in why                                          # too far above their price
    assert decide(10, 1.1, 1.0, 25.0, 1.0, [], c)[0] == "wait"                     # spiking: not the top
    assert decide(10, 1.1, 1.0, 2.0, 1.0, ["SELLER"], c)[0] == "sold"              # they leave: we don't go in
    assert decide(10, 1.1, 1.0, 2.0, 0.5, [], c)[0] == "cancel"                    # liquidity leaving
    assert decide(3601, 1.5, 1.0, 2.0, 1.0, [], c)[0] == "skip"                    # 60 min without the price


def _whale_desk(tmp_path):
    from desk.agents.executor import Executor
    from desk.agents.risk import Risk
    from desk.brokers.paper import PaperBroker
    from tests.test_entry import FakeDex, FakeJup
    from tests.test_rules import make_desk

    class Smart:
        def __init__(self):
            self.reports = {"M": {"wallets": ["SMARTWALLET"], "entry_sol": 0.00001}}
            self.sellers: list[str] = []

        async def sold(self, mint, wallets):
            return [w for w in wallets if w in self.sellers]

    desk = make_desk(tmp_path)
    desk.cfg.entry.mode = "now"
    dex = FakeDex()                       # price_native = price_usd / 100
    desk.jup, desk.dex, desk.smart = FakeJup(), dex, Smart()
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    return desk, dex, ex


def _whale_verdict(desk):
    from tests.test_rules import signed_verdict
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6
    v.features = {"whale_free_score": 0.3, "whale_holders": 1, "whale_entry_sol": 0.00001}
    return v


def test_executor_waits_for_the_smart_price_then_buys(tmp_path):
    desk, dex, ex = _whale_desk(tmp_path)
    dex.price = 0.0015                    # 0.000015 SOL = +50% over the smart wallets' 0.00001

    async def go():
        await ex.handle_verdict(_whale_verdict(desk))
        assert "M" not in desk.portfolio.positions and "M" in ex.whale_plans
        p = ex.whale_plans["M"]
        dex.price = 0.0012                # +20%: the price is right
        assert await ex.whale_look("M", p, await dex.snapshot("M"), p["t0"] + 60) == "buy"
        await ex.handle_verdict(_whale_verdict(desk))
    asyncio.run(go())
    assert "M" in desk.portfolio.positions and "wallet bravi" in desk.portfolio.positions["M"].entry_reason


def test_executor_drops_token_when_the_smart_wallet_sells(tmp_path):
    desk, dex, ex = _whale_desk(tmp_path)
    dex.price = 0.0016

    async def go():
        await ex.handle_verdict(_whale_verdict(desk))
        p = ex.whale_plans["M"]
        desk.smart.sellers = ["SMARTWALLET"]
        dex.price = 0.0011                # even at a good price: they sold, we don't buy
        p["sold_check"] = 0
        assert await ex.whale_look("M", p, await dex.snapshot("M"), p["t0"] + 60) == "sold"
        assert "M" not in ex.whale_plans and "M" in ex.whale_blocked
        await ex.handle_verdict(_whale_verdict(desk))                  # re-approved: still dropped
    asyncio.run(go())
    assert "M" not in desk.portfolio.positions
    last = desk.store.q("SELECT reason FROM decisions WHERE stage='executor' ORDER BY id DESC LIMIT 1")[0]["reason"]
    assert last.startswith("a smart wallet sold")


def test_token_without_smart_wallets_keeps_the_usual_entry(tmp_path):
    from tests.test_rules import signed_verdict
    desk, dex, ex = _whale_desk(tmp_path)
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6
    asyncio.run(ex.handle_verdict(v))                                   # entry mode "now": bought at once
    assert "M" in desk.portfolio.positions and not ex.whale_plans
