"""GMGN integration: parsing, cluster detection, filter kills, rate-limit handling."""
from __future__ import annotations

import asyncio
import time

import pytest

from desk.agents.analyst import score_token
from desk.agents.filter import Filter
from desk.clients.gmgn import WeightedBucket, dev_history, funding_clusters, summarize
from desk.models import Check
from tests.test_rules import make_desk, snap

MINT = "Mint1111"


def holder(addr, pct, funder=None, ts=0, amt=1.0, addr_type=0):
    h = {"address": addr, "addr_type": addr_type, "amount_percentage": pct}
    if funder:
        h["native_transfer"] = {"from_address": funder, "timestamp": ts, "amount": amt}
    return h


def test_funding_clusters_detects_same_funder_whale():
    hs = [holder("pool", 0.3, addr_type=2), holder(MINT, 0.05)]
    hs += [holder(f"w{i}", 0.02, "F1", ts=1_000 + i * 600, amt=1.0) for i in range(5)]   # coherent: 10%
    hs += [holder(f"x{i}", 0.01, f"solo{i}") for i in range(10)]
    fc = funding_clusters(hs, MINT)
    assert fc["cluster_pct"] == pytest.approx(10.0) and fc["clusters"][0]["wallets"] == 5
    assert fc["wallets_seen"] == 15      # pool and the token's own account excluded


def test_funding_clusters_scripted_funding_flag():
    hs = [holder(f"w{i}", 0.001, f"f{i}", ts=5_000 + i * 10) for i in range(4)]  # 4 fundings in 30s
    assert funding_clusters(hs, MINT)["synced_funding"]
    hs = [holder(f"w{i}", 0.001, f"f{i}", ts=5_000 + i * 200) for i in range(4)]
    assert not funding_clusters(hs, MINT)["synced_funding"]


def test_incoherent_group_not_counted():
    hs = [holder(f"w{i}", 0.05, "F", ts=i * 86_400, amt=amt) for i, amt in enumerate((0.1, 5, 40))]
    assert funding_clusters(hs, MINT)["cluster_pct"] == 0


def test_summarize_missing_is_unknown_not_safe():
    s = summarize({}, {})
    assert s["rug_ratio"] is None and s["insider_rate"] is None and s["honeypot"] is None
    s = summarize({"stat": {"top70_sniper_hold_rate": "0.42"}, "dev": {"fund_from": "FUNDER"}},
                  {"rug_ratio": 0.8, "honeypot": 1})
    assert s["sniper_rate"] == 0.42 and s["dev_funder"] == "FUNDER" and s["rug_ratio"] == 0.8


def test_dev_history():
    assert dev_history({"inner_count": 40, "open_count": 1})["launches"] == 41
    assert dev_history({"inner_count": 40, "open_count": 1})["open_ratio"] == pytest.approx(1 / 41)
    assert dev_history(None)["launches"] == 0


class FakeGmgn:
    def __init__(self, info=None, sec=None, created=None, holders=None):
        self.info, self.sec, self.created, self.holders = info or {}, sec or {}, created, holders or []
        self.holder_calls = 0

    async def token_info(self, mint):
        return self.info

    async def token_security(self, mint):
        return self.sec

    async def created_tokens(self, wallet):
        return self.created

    async def top_holders(self, mint, limit=100):
        self.holder_calls += 1
        return self.holders


def run_checks(tmp_path, gm, creator="DEV"):
    desk = make_desk(tmp_path)
    desk.gmgn = gm
    f = Filter(desk)
    checks: list[Check] = []
    s = asyncio.run(f.gmgn_checks(MINT, "TST", creator, checks))
    return desk, checks, s


def test_gmgn_kills(tmp_path):
    _, checks, _ = run_checks(tmp_path, FakeGmgn(sec={"rug_ratio": 0.9, "honeypot": 1}))
    failed = {c.name: c for c in checks if not c.passed}
    assert failed["gmgn_honeypot"].permanent and failed["gmgn_rug_label"].permanent


def test_gmgn_serial_dev_and_funder_ledger(tmp_path):
    desk = make_desk(tmp_path)
    desk.store.add_rugger("BADFUNDER", "old", "rugged before")
    desk.gmgn = FakeGmgn(info={"dev": {"fund_from": "BADFUNDER"}}, created={"inner_count": 60, "open_count": 1})
    checks: list[Check] = []
    asyncio.run(Filter(desk).gmgn_checks(MINT, "TST", "DEV", checks))
    failed = {c.name for c in checks if not c.passed}
    assert {"dev_funder_rug_link", "dev_serial_launcher"} <= failed
    assert "DEV" in desk.store.ruggers()          # serial launcher added to the rug ledger


def test_gmgn_clean_token_passes_and_checks_clusters(tmp_path):
    gm = FakeGmgn(info={"stat": {"top70_sniper_hold_rate": 0.05}, "wallet_tags_stat": {"smart_wallets": 6}},
                  sec={"rug_ratio": 0.0, "honeypot": 0, "suspected_insider_hold_rate": 0.02},
                  created={"inner_count": 2, "open_count": 1},
                  holders=[holder(f"w{i}", 0.01, f"f{i}") for i in range(20)])
    _, checks, s = run_checks(tmp_path, gm)
    assert all(c.passed for c in checks) and gm.holder_calls == 1
    assert s["cluster_pct"] == 0 and s["smart_wallets"] == 6


def test_gmgn_cluster_reject(tmp_path):
    gm = FakeGmgn(holders=[holder(f"w{i}", 0.04, "F", ts=100 + i) for i in range(6)])   # 24% one funder
    _, checks, _ = run_checks(tmp_path, gm)
    assert any(c.name == "funding_clusters" and not c.passed for c in checks)


def test_rate_limit_block_pauses_all_calls():
    async def go():
        b = WeightedBucket(rate=100, capacity=100)
        b.block(time.time() + 0.3)
        t = time.monotonic()
        await b.take(1)
        return time.monotonic() - t
    assert asyncio.run(go()) >= 0.25


def test_smart_money_component_and_scripted_cap():
    web = {"url": "https://example.org", "score": 0.8, "legit": 0.8, "tasty": 0.7, "flags": []}
    base = {"top10_pct": 9, "holders_total": 2500, "insider_pct": 3, "risks": []}
    plain, b0, _ = score_token(snap(), base, web)
    smart, b1, _ = score_token(snap(), {**base, "gmgn": {"smart_wallets": 10, "renowned_wallets": 2}}, web)
    assert "smart_money" not in b0 and b1["smart_money"] == 1.0 and smart > plain
    synced, _, why = score_token(snap(), {**base, "gmgn": {"synced_funding": True}}, web)
    assert synced <= 0.45 and any("script" in w for w in why)
