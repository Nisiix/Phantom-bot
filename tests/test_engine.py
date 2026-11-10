"""Engine: two-stage Filter, concurrent exits, ghost positions, QUANT cache, signal research."""
from __future__ import annotations

import asyncio
import json
import random
import time

from desk.agents.executor import Executor
from desk.agents.filter import Filter, deep_priority
from desk.agents.risk import Risk
from desk.brokers.paper import PaperBroker
from desk.models import Candidate
from desk.quant import QuantModel
from desk.research import build_research, market_features, signal_table, parse_rows
from tests.test_quant import outcome
from tests.test_rules import make_desk, position, signed_verdict, snap


def test_filter_screen_and_priority(tmp_path):
    f = Filter(make_desk(tmp_path))
    low = f.screen(Candidate("A"), snap(mint="A", volume_h1=10_000))
    assert low is not None and not low.passed and low.failed[0].name == "volume_1h"
    assert f.screen(Candidate("Bpump"), snap(mint="Bpump")) is None         # goes on to deep checks
    assert not f.screen(Candidate("C"), None).passed                  # no market at all
    grad = deep_priority(Candidate("G", source="pumpfun:migration"), snap(volume_h1=60_000))
    busy = deep_priority(Candidate("H", source="pumpfun:new"), snap(volume_h1=900_000))
    quiet = deep_priority(Candidate("I", source="pumpfun:new"), snap(volume_h1=60_000))
    assert grad < busy < quiet


def test_rug_ledger_single_lookup(tmp_path):
    desk = make_desk(tmp_path)
    desk.store.add_rugger("DEV", "m", "rugged")
    assert desk.store.rugger("DEV")["reason"] == "rugged"
    assert desk.store.rugger("OTHER") is None and desk.store.rugger(None) is None


def test_closed_position_is_never_persisted_again(tmp_path):
    desk = make_desk(tmp_path)
    pf = desk.portfolio
    pos = position()
    pf.open(pos)
    pf.close("M", 1.0, "test")
    pf.persist(pos)                      # e.g. Risk finishing a tick after the Executor sold
    assert desk.store.load_positions() == []


def test_risk_recheck_before_swap_blocks_entry_after_concurrent_loss(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)

    class FakeJup:
        async def quote(self, i, o, amount, bps):
            if i.startswith("So111"):
                return {"inAmount": str(amount), "outAmount": str(int(amount / 1e9 / 0.00001 * 1e6))}
            risk.pause_until = time.time() + 600  # a losing streak paused entries while we were quoting
            return {"outAmount": str(int(int(amount) * 0.00001 / 1e6 * 1e9 * 0.98))}

    class FakeDex:
        async def snapshot(self, mint, fresh=True):
            return snap(mint=mint)

    desk.jup, desk.dex = FakeJup(), FakeDex()
    ex = Executor(desk, PaperBroker(desk.portfolio), risk)
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6
    asyncio.run(ex.handle_verdict(v))
    assert "M" not in desk.portfolio.positions
    assert ex.state.counters.get("declined") == 1


def test_own_order_not_counted_twice(tmp_path):
    desk = make_desk(tmp_path)
    desk.cfg.executor.max_open_positions = 1
    risk = Risk(desk)
    desk.in_flight.add("M")
    assert not risk.approve_entry("M")[0]                 # someone else's order in flight
    assert risk.approve_entry("M", own_order=True)[0]     # the Executor re-checking its own buy


def test_exit_runs_while_entry_is_busy(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)
    entry_started, release = asyncio.Event(), asyncio.Event()
    sold = []

    class SlowDex:
        async def snapshot(self, mint, fresh=True):
            entry_started.set()
            await release.wait()                 # entry stuck on market data
            return None

    class FakeJup:
        async def quote(self, i, o, amount, bps, lane=""):
            return {"outAmount": str(10**9)}

    class Broker(PaperBroker):
        async def sell(self, mint, tokens, decimals, quote):
            sold.append(mint)
            return await super().sell(mint, tokens, decimals, quote)

    async def go():
        from desk.models import ExitOrder
        desk.jup, desk.dex = FakeJup(), SlowDex()
        desk.portfolio.open(position())
        ex = Executor(desk, Broker(desk.portfolio), risk)
        v = signed_verdict(desk, mint="N")
        v.clearance.report.rug["decimals"] = 6
        await desk.verdicts.put(v)
        task = asyncio.create_task(ex.run())
        await asyncio.wait_for(entry_started.wait(), 2)
        await desk.exits.put(ExitOrder("M", "HARD_STOP", "test"))
        for _ in range(100):
            if sold:
                break
            await asyncio.sleep(0.01)
        release.set()
        desk.stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert sold == ["M"] and "M" not in desk.portfolio.positions


def test_quant_cache_follows_new_outcomes():
    q = QuantModel()
    q.load([outcome("up", 14) for _ in range(5)])
    p1, _, _ = q.probabilities(0.65)
    for _ in range(40):
        q.add(outcome("down", -7))
    p2, _, n = q.probabilities(0.65)
    assert p2["down"] > p1["down"] and n == 45


def _row(i, f, label, ret):
    return {"start_ts": 1_700_000_000 + i * 600, "end_ts": 0, "score": f.get("score"), "desk_label": label,
            "model_label": "neither", "sim_net_pct": ret, "source": "pumpfun:new", "features": json.dumps(f)}


def test_research_finds_a_real_signal_and_flags_noise():
    rnd = random.Random(7)
    rows = []
    for i in range(300):
        bp = rnd.random()                              # buy share: drives the outcome
        junk = rnd.random()                            # unrelated
        win = rnd.random() < 0.15 + 0.6 * bp
        rows.append(_row(i, {"buy_ratio_h1": bp, "chg_m5": junk, "score": 0.5 + bp / 2},
                         "up" if win else "down", 10.0 if win else -7.0))
    table = {t["key"]: t for t in signal_table(parse_rows(rows))}
    assert not table["buy_ratio_h1"]["noise"] and table["buy_ratio_h1"]["spread"] > 5
    assert table["chg_m5"]["noise"]
    text = build_research(rows)
    assert "buy share 1h" in text and "score >= 0.60" in text
    assert "Not enough data" in build_research(rows[:5])


def test_shadow_rows_keep_features(tmp_path):
    from desk.shadow import ShadowTracker
    desk = make_desk(tmp_path)
    st = ShadowTracker(desk, lambda *a, **k: None)
    st.start("M", "TST", "analyst", "approve", "r", 0.001, 80_000, score=0.7,
             features=market_features(snap(), "pumpfun:new"))
    s = st.active["M:analyst"]
    s.desk_label = s.model_label = "neither"
    s.sim_ret = 0.0
    st._store(s)
    rows = desk.store.shadow_research_rows("analyst")
    assert json.loads(rows[0]["features"])["source"] == "pumpfun:new"


def test_market_regime_snapshot_and_crash_flag(tmp_path):
    from desk.market import BTC_MINT, MEME_BASKET, MarketRegime, is_crash
    from desk.clients.jupiter import SOL_MINT
    mr = MarketRegime()
    t0 = 1_700_000_000.0
    memes = list(MEME_BASKET)
    for i in range(61):                       # one sample a minute for an hour
        t = t0 + i * 60
        btc = 100_000 * (1 - 0.03 * i / 60)  # BTC -3% over the hour
        prices = {BTC_MINT: {"usdPrice": btc, "priceChange24h": -4.0},
                  SOL_MINT: {"usdPrice": 150.0, "priceChange24h": 1.0}}
        prices.update({m: {"usdPrice": 1.0 + 0.01 * i / 60, "priceChange24h": 2.0} for m in memes})
        mr.record(prices, t)
    for i in range(12):
        mr.note_launch(10.0 + i, 80_000.0, t)
    s = mr.snapshot(t)
    assert round(s["mkt_btc_h1"], 1) == -3.0 and s["mkt_sol_h1"] == 0.0
    assert round(s["mkt_meme_h1"], 1) == 1.0 and s["mkt_btc_h24"] == -4.0
    assert s["mkt_launch_h1"] == 15.5 and s["mkt_launch_vol"] == 80_000.0
    assert is_crash(s) is True
    assert is_crash({**s, "mkt_btc_h1": -0.5}) is False
    assert is_crash({}) is None
    # Less than an hour of history: no 1h change rather than a wrong one.
    young = MarketRegime()
    young.record({BTC_MINT: {"usdPrice": 1.0}}, t0)
    young.record({BTC_MINT: {"usdPrice": 2.0}}, t0 + 600)
    assert young.snapshot(t0 + 600)["mkt_btc_h1"] is None


def test_research_reports_outcomes_by_market_regime():
    rnd = random.Random(3)
    rows = []
    for i in range(240):
        crash = i % 4 == 0
        f = {"score": 0.7, "mkt_btc_h1": -3.0 if crash else rnd.uniform(-1, 1), "mkt_sol_h1": 0.0,
             "mkt_meme_h1": 0.0}
        win = rnd.random() < (0.1 if crash else 0.5)
        rows.append(_row(i, f, "up" if win else "down", 10.0 if win else -7.0))
    trades = [{"pnl_pct": -6.0, "entry_ts": 1, "data": json.dumps({"regime": {"mkt_btc_h1": -3.0}})},
              {"pnl_pct": 12.0, "entry_ts": 1, "data": json.dumps({"regime": {"mkt_btc_h1": 0.2}})},
              {"pnl_pct": 5.0, "entry_ts": 1, "data": "{}"}]
    text = build_research(rows, trades=trades)
    assert "By market regime" in text and "crash hours" in text and "CLEAR difference" in text
    assert "Real trades by market regime" in text
    old = [_row(i, {"score": 0.7}, "up", 10.0) for i in range(40)]
    assert "No regime data yet" in build_research(old)


def test_shadow_rows_carry_market_regime(tmp_path):
    from desk.market import MarketRegime
    from desk.shadow import ShadowTracker
    desk = make_desk(tmp_path)
    desk.market = MarketRegime()
    st = ShadowTracker(desk, lambda *a, **k: None)
    st.start("M", "TST", "filter", "reject", "r", 0.001, 80_000, features=market_features(snap(), "x"))
    f = st.active["M:filter"].features
    assert "mkt_btc_h1" in f and len(desk.market.launches) == 1


def test_pump_fun_only_venue_scope(tmp_path):
    from desk.clients.feeds import TRACKED_DEXES, is_pump_token, venue_dexes
    assert venue_dexes("pumpfun") == ("pump-fun", "pumpswap") and venue_dexes("all") == TRACKED_DEXES
    assert is_pump_token("Abcpump", "raydium") and not is_pump_token("Xyz", "pumpswap")
    assert not is_pump_token("Xyzbonk", "raydium")
    desk = make_desk(tmp_path)
    f = Filter(desk)
    assert f.screen(Candidate("Okpump"), snap(mint="Okpump", dex_id="raydium")) is None
    out = f.screen(Candidate("Ray"), snap(mint="Ray", dex_id="raydium"))
    assert out is not None and [c.name for c in out.failed] == ["venue"] and "Ray" in desk.blacklist
    cc = f.screen(Candidate("Copy"), snap(mint="Copy", dex_id="pumpswap"))     # copycat pooled on PumpSwap
    assert cc is not None and [c.name for c in cc.failed] == ["venue"] and "copycat" in desk.blacklist["Copy"]
    desk.cfg.scout.venues = "all"
    assert f.screen(Candidate("Ray2"), snap(mint="Ray2", dex_id="raydium")) is None


def test_graduation_wait_audit_follows_tokens_from_wait_end(tmp_path):
    from desk.shadow import ShadowTracker
    desk = make_desk(tmp_path)
    st = ShadowTracker(desk, lambda *a, **k: None)
    t = time.time()
    st.defer_graduation("Alivepump", "ALIVE", t - 50 * 60, 45, "graduation_wait: graduated 2 min ago")
    st.defer_graduation("Deadpump", "DEAD", t - 50 * 60, 45, "graduation_wait")
    st.defer_graduation("Laterpump", "LATER", t - 10 * 60, 45, "graduation_wait")
    st.defer_graduation("Alivepump", "ALIVE", t - 50 * 60, 45, "dup")          # no duplicates
    assert len(st.deferred) == 3
    alive = snap(mint="Alivepump", liquidity_usd=80_000)
    for _ in range(3):                                   # dead token: no price on 3 ticks in a row
        st.start_due({"Alivepump": alive}, t)
    assert "Alivepump:grad45" in st.active and st.active["Alivepump:grad45"].decision == "buy_at_wait_end"
    assert "Laterpump" in st.deferred and "Deadpump" not in st.deferred
    gone = desk.store.q("SELECT decision, stage FROM shadow WHERE mint='Deadpump'")
    assert gone == [{"decision": "gone_at_wait_end", "stage": "grad45"}]
    st.save()                                            # pending audits survive a restart
    st2 = ShadowTracker(desk, lambda *a, **k: None)
    assert "Laterpump" in st2.deferred
