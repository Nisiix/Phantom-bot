"""2026-11-14 (user): rejected tokens followed for 24 h, smart wallets measured during the graduation wait,
QUANT labels that follow the exits in use."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace as NS

from desk import longwatch as lw
from desk import whale_audit as wa
from desk.config import Config, SmartWalletsCfg, enforce_hard_limits
from desk.models import Candidate, Check, FilterReport
from desk.quant import barriers, learning_rows, model_from_cfg, trade_label
from desk.store import Store
from tests.test_rules import make_desk, snap

H = 3600.0


def grad(minutes=10, **kw):
    kw.setdefault("market_cap", 60_000)
    kw.setdefault("liquidity_usd", 25_000)
    return snap(pair_created_at=time.time() - minutes * 60, **kw)


# ── step 1: what became of the rejected tokens ──
def test_longwatch_follows_buyable_graduated_rejects(tmp_path):
    desk = make_desk(tmp_path)
    w = lw.LongWatch(desk)
    t0 = time.time() - 25 * H
    assert w.note("Apump", "A", grad(), ["graduation_wait"], now=t0)
    assert w.note("Apump", "A", grad(), ["funder_rug_link"], kill=True, now=t0 + 60)       # same token: rules add up
    row = desk.store.q("SELECT * FROM longwatch WHERE mint='Apump'")[0]
    assert row["rules"] == "funder_rug_link,graduation_wait" and row["decision"] == "kill"
    assert row["ref_mcap"] == 60_000
    # not followed: out of scope, not graduated, too small
    assert not w.note("Bpump", "B", grad(), ["venue", "volume_1h"])
    assert not w.note("Cpump", "C", snap(dex_id="pumpfun", market_cap=60_000), ["volume_1h"])
    assert not w.note("Dpump", "D", grad(market_cap=60_000, liquidity_usd=1_000), ["volume_1h"])
    assert w.note("Epump", "E", grad(), ["volume_1h"], now=t0)
    assert w.note("Fpump", "F", grad(), ["analyst_score"], now=time.time() - 60)           # too young to judge

    # 3 h in: A pumps to $150k on real liquidity; a dust-pool reading ($9M on $800) is not a peak
    w.update({"Apump": grad(market_cap=150_000, price_usd=0.0025, liquidity_usd=40_000),
              "Epump": grad(market_cap=9_000_000, price_usd=0.15, liquidity_usd=800)}, now=t0 + 3 * H)
    # 6 h: A alive, E dead; 24 h: A dead too
    w.update({"Apump": grad(market_cap=90_000, liquidity_usd=30_000),
              "Epump": grad(market_cap=3_000, liquidity_usd=900)}, now=t0 + 6.1 * H)
    assert w.update({"Apump": grad(market_cap=4_000, liquidity_usd=1_500)}, now=t0 + 24.1 * H) == 2
    a = desk.store.q("SELECT * FROM longwatch WHERE mint='Apump'")[0]
    e = desk.store.q("SELECT * FROM longwatch WHERE mint='Epump'")[0]
    assert a["peak_mcap"] == 150_000 and a["mcap_6h"] == 90_000 and a["mcap_24h"] == 4_000 and a["done"] == 1
    assert e["peak_mcap"] == 60_000 and e["done"] == 1 and e["mcap_24h"] == 0
    assert "Apump" not in w.rules and not w.note("Apump", "A", grad(), ["volume_1h"])        # one row per token

    cfg = desk.cfg
    s = lw.summary(lw.load(desk.store.path), cfg)
    assert s["total"] == {"n": 3, "n6": 2, "reached": 1, "alive6": 1, "n24": 2, "alive24": 0}
    assert s["rules"]["funder_rug_link"]["reached"] == 1 and s["rules"]["volume_1h"]["reached"] == 0
    assert s["alone"]["funder_rug_link"]["n6"] == 1 and "graduation_wait" not in s["alone"]
    text = lw.report_section(s, cfg)
    assert "finanziatore legato a un rug: 1 token, arrivati 1 (100%), vivi a 6 ore 1 (100%)" in text
    assert "vivi a 24 ore 0 su 1" in text and "pochi dati" in text
    assert "nessun token seguito ancora" in lw.report_section(None, cfg)
    # a token the desk bought in the end was not missed
    desk.store.db.execute("INSERT INTO positions VALUES('Apump','{}')")
    assert lw.summary(lw.load(desk.store.path), cfg)["total"]["reached"] == 0
    # followed tokens survive a restart
    assert set(lw.LongWatch(desk).rules) == {"Fpump"}


def test_filter_reports_its_rejections_to_the_long_watch(tmp_path):
    from desk.agents.filter import Filter
    desk = make_desk(tmp_path)
    f = Filter(desk)
    desk.longwatch = lw.LongWatch(desk)
    m = grad(mint="Zpump")
    rep = FilterReport("Zpump", "Z", False, [Check("volume_1h", False, 1, 2, "low"), Check("liquidity", True)],
                       market=m)
    asyncio.run(f.route(Candidate("Zpump"), rep))
    assert desk.store.q("SELECT rules FROM longwatch WHERE mint='Zpump'")[0]["rules"] == "volume_1h"


# ── step 5: smart wallets during the graduation wait (measurement only) ──
def test_whale_checker_watches_the_wait_only_with_known_wallets():
    from desk.agents.whale import WhaleChecker
    from desk.hub import Hub
    sh = NS(active={}, deferred={"Wpump": {"mint": "Wpump", "symbol": "W", "grad": 5.0},
                                 "Vpump": {"mint": "Vpump", "symbol": "V", "grad": 9.0},
                                 "Ppump": {"mint": "Ppump", "symbol": "P", "grad": 7.0}})
    desk = NS(cfg=NS(smart_wallets=SmartWalletsCfg(watch_wait_max_tokens=1)), hub=Hub(), shadow=sh,
              portfolio=NS(positions={"Ppump": NS(symbol="P")}))
    wc = WhaleChecker(desk)
    wc.sw = NS(smart={})
    assert [(m, st) for m, _, st in wc.watched()] == [("Ppump", "position")]          # no known wallet: nothing
    wc.sw = NS(smart={"SMART": {}})
    assert [(m, st) for m, _, st in wc.watched()] == [("Ppump", "position"), ("Vpump", "wait")]   # newest, capped


def test_watch_wait_stores_its_own_rows(tmp_path):
    from desk.smartwallets import SmartWallets
    st = Store(tmp_path / "p.db")
    sw = SmartWallets(st, None, None, SmartWalletsCfg(rpc_interval_seconds=0))
    assert asyncio.run(sw.watch_wait("Wpump", "W")) == [] and not st.q("SELECT * FROM sw_wait_seen")
    sw.smart = {"SMART": {"wins": 4, "tokens": 6}, "OTHER": {"wins": 3, "tokens": 5}}

    async def holders(mint, max_age=60.0):
        return {"SMART": 1000.0} if mint == "Wpump" else {}
    sw.holders = holders
    assert asyncio.run(sw.watch_wait("Wpump", "W")) == ["SMART"]
    assert asyncio.run(sw.watch_wait("Wpump", "W")) == []                 # one row per wallet and token
    asyncio.run(sw.watch_wait("Npump", "N"))
    seen = {r["mint"]: r for r in st.q("SELECT * FROM sw_wait_seen")}
    assert seen["Wpump"]["checks"] == 2 and seen["Npump"]["checks"] == 1
    assert [(r["mint"], r["wallet"], r["wins"]) for r in st.q("SELECT * FROM sw_wait_hits")] == [("Wpump", "SMART", 4)]
    assert not st.q("SELECT * FROM sw_hits")                              # the Analyst's hits are untouched

    seen_rows, hit = wa.wait_load(st.path)
    frows = [{"mint": "Wpump", "ret": 12.0}, {"mint": "Wpump", "ret": -50.0}, {"mint": "Npump", "ret": -30.0}]
    long_rows = [{"mint": "Wpump", "mcap_6h": 1, "peak_mcap": 150_000, "ref_price": 1.0, "peak_price": 1.5},
                 {"mint": "Npump", "mcap_6h": 1, "peak_mcap": 40_000, "ref_price": 1.0, "peak_price": 1.1}]
    a = wa.wait_audit(seen_rows, hit, frows, long_rows, 100_000)
    assert a["with"]["tokens"] == 1 and a["with"]["mean"] == 12.0 and a["with"]["reached"] == 1
    assert a["without"]["mean"] == -30.0 and a["without"]["reached"] == 0
    cfg = Config()
    text = wa.wait_section(a, cfg, 2)
    assert "dati insufficienti" in text and "con un wallet bravo dentro: 1 token" in text
    assert "controllo fermo" in wa.wait_section(None, cfg, 0)
    big = {**a, "with": {**a["with"], "n": 40, "win": 0.85, "lo": 4.0}}
    assert "decide l'utente" in wa.wait_section(big, cfg, 2)
    assert "l'attesa resta com'è" in wa.wait_section({**big, "with": {**big["with"], "win": 0.6}}, cfg, 2)


# ── QUANT follows the exits in use ──
def test_quant_barriers_follow_the_exits_in_use(tmp_path):
    cfg = Config()
    cfg.risk.trail_activate_pct, cfg.risk.hard_stop_pct, cfg.risk.profit_lock_activate_pct = 50.0, -4.0, 30.0
    enforce_hard_limits(cfg)
    assert barriers(cfg.risk) == (50.0, -4.0)                 # with a loss stop: as always
    cfg.risk.loss_stop = False
    enforce_hard_limits(cfg)
    assert barriers(cfg.risk) == (30.0, -100.0)               # the profit stop arms / total loss
    o = {"label": "down", "ret_net_pct": 23.0}
    assert trade_label(o, cfg) == "up"                        # dipped to -4% under the old stop, sold at +23%
    assert trade_label({"label": "up", "ret_net_pct": -100.0}, cfg) == "down"
    assert trade_label({"label": "down", "ret_net_pct": -20.0}, cfg) == "neither"
    cfg.risk.loss_stop = True
    assert trade_label(o, cfg) == "down"

    cfg.risk.loss_stop = False
    cfg.data_dir = tmp_path
    st = Store(tmp_path / "paper.db")
    base = {"mode": "paper", "mint": "Apump", "symbol": "A", "score": 0.7, "rt_cost_pct": 2.0, "p_up": .3,
            "p_down": .4, "p_neither": .3, "ev_pct": 0.0}
    st.add_outcome({**base, "ts": time.time(), "label": "down", "ret_net_pct": 23.0, "gross_pct": 25.0})
    st.add_outcome({**base, "ts": 1.0, "label": "down", "ret_net_pct": -4.0, "gross_pct": -2.0})   # before learn_since
    rows = learning_rows([st], cfg)
    assert [(r["label"], r["ret_net_pct"]) for r in rows] == [("up", 23.0)]
    assert st.outcomes()[-1]["label"] == "down"               # the stored row is not changed
    m = model_from_cfg(cfg)
    assert m.prior_r["down"] == -100.0 and abs(sum(m.prior_p.values()) - 1) < 1e-9
    assert abs(sum(m.prior_p[k] * m.prior_r[k] for k in m.prior_p) - 1.275) < 0.05   # same edge as the old priors
    cfg.risk.loss_stop = True
    assert model_from_cfg(cfg).prior_r["down"] == -6.5
