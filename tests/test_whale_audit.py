"""The Controller measures how precise the Whale checker is (user 2026-11-12, desk/whale_audit.py)."""
from __future__ import annotations

import time

from desk import whale_audit as wa
from desk.config import Config
from desk.store import Store


def row(mint, ret, holders=0, bonus=0.0, score=0.5, ready=True, ts=None):
    return {"mint": mint, "ret": ret, "ts": ts or time.time() - 3 * 86400,
            "f": {"whale_ready": ready, "whale_holders": holders, "whale_bonus": bonus, "score": score}}


def test_groups_promoted_and_precision():
    now = time.time()
    rows = [row(f"W{i}", 8.0 + i % 3, holders=1, bonus=0.05, score=0.62) for i in range(20)] + \
           [row(f"N{i}", -2.0 + i % 3, holders=0) for i in range(30)] + \
           [row("OLD", 50.0, ready=None), row("NR", 50.0, ready=False)]          # not ready: never counted
    hits = [{"mint": f"W{i}", "wallet": "SMART" if i < 10 else "MEH", "ts": now - 2 * 86400, "stage": "analyst"}
            for i in range(20)]
    tokens = {f"W{i}": {"peak": 300_000 if i < 8 else 50_000, "first_ts": now - 2 * 86400} for i in range(20)}
    tokens.update({f"N{i}": {"peak": 300_000 if i < 3 else 20_000, "first_ts": now - 2 * 86400} for i in range(30)})
    a = wa.audit(rows, hits, tokens, 0.6, 200_000, 24, now)
    assert a["ready_rows"] == 50 and a["with"]["n"] == 20 and a["without"]["n"] == 30
    assert a["promoted"]["n"] == 20 and a["promoted"]["lo"] > 0             # 0.62 - 0.05 < 0.6 <= 0.62
    assert a["diff"]["ok"] and a["diff"]["lo"] > 0
    assert a["precision"] == 8 / 20 and abs(a["base_rate"] - 11 / 52) < 1e-9 and a["lift"] > 1.5
    assert a["wallets"][0][0] == "SMART" and a["wallets"][0][1:] == (8, 10)
    cfg = Config()
    props = wa.proposals(a, cfg)
    assert props and props[0]["changes"][0] == "[analyst] whale_bonus_per_wallet = 0.075"
    text = wa.report_section(a, cfg, {"ready": True, "why": "6 smart wallets"})
    assert "MEGLIO" in text and "promossi dal bonus: 20 token" in text and "1.9 volte la base" in text


def test_losing_bonus_is_proposed_off_and_young_tokens_wait():
    now = time.time()
    rows = [row(f"P{i}", -5.0 - i % 4, holders=2, bonus=0.10, score=0.65) for i in range(16)]
    a = wa.audit(rows, [{"mint": "YOUNG", "wallet": "X", "ts": now - 60, "stage": "analyst"}], {}, 0.6, 200_000,
                 24, now)
    assert a["hit_decided"] == 0 and a["precision"] is None                  # 1 minute old: not judged yet
    props = wa.proposals(a, Config())
    assert [p["changes"] for p in props] == [["[analyst] whale_bonus_per_wallet = 0"]]
    assert props[0]["gain"] > 0 and props[0]["verdict"] == "solida"


def test_few_rows_no_proposal_and_report_says_so():
    a = wa.audit([row("A", 3.0, holders=1, bonus=0.05, score=0.61)], [], {}, 0.6, 200_000, 24)
    assert wa.proposals(a, Config()) == []
    assert "pochi dati" in wa.report_section(a, Config())
    assert "nessun dato" in wa.report_section(None, Config())


def test_load_reads_hits_and_real_peaks(tmp_path):
    from desk.smartwallets import SmartWallets
    from tests.test_smartwallets import Rpc, cfg
    st = Store(tmp_path / "p.db")
    assert wa.load(st.path, {"Xpump"}) == ([], {})                          # Whale checker never ran
    SmartWallets(st, Rpc({}), None, cfg())                                  # creates its tables
    st.db.execute("INSERT INTO sw_hits VALUES('Xpump','W','X',1,'analyst',1,5,3,4)")
    ins = "INSERT INTO decisions(ts,stage,outcome,mint,symbol,reason,data) VALUES(?,?,?,?,?,?,?)"
    st.db.execute(ins, (1, "filter", "reject", "Xpump", "X", "", '{"checks": ["FAIL liquidity: 2000 vs 1", '
                                                                 '"PASS market_cap: 1 vs 1 (market cap $9,000,000)"]}'))
    st.db.execute(ins, (2, "filter", "pass", "Xpump", "X", "", '{"checks": ["PASS liquidity: 40000 vs 1", '
                                                               '"PASS market_cap: 1 vs 1 (market cap $260,000)"]}'))
    hits, tokens = wa.load(st.path, set())
    assert hits[0]["wallet"] == "W" and tokens["Xpump"]["peak"] == 260_000   # the dust-pool reading is ignored
