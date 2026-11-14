"""Quarantine instead of a permanent kill for soft evidence (user 2026-11-14): a funder link or an unproven
name holds the token back; it is set aside only 60 min after graduation with the pool intact and more holders."""
import asyncio
import time

from desk.funding import soft_ledger_reason
from desk.models import Candidate

from tests.test_rules import _filter_with_rugcheck, _identity_filter, _Probe, snap


def _funder_filter(tmp_path, holders=100):
    desk, f = _filter_with_rugcheck(tmp_path, 1.0)
    base = desk.rug.report
    state = {"holders": holders}

    async def report(mint):
        r = await base(mint)
        r["totalHolders"] = state["holders"]
        return r
    desk.rug.report = report
    st = desk.store
    st.add_rugger("OLDDEV", "OLDpump", "liquidity collapsed 30,000 -> 900 USD")
    st.save_wallet_funder({"wallet": "OLDDEV", "funder": "BOSS", "complete": True, "hub": False})
    st.save_wallet_funder({"wallet": "DEV", "funder": "BOSS", "complete": True, "hub": False})
    st.save_wallet_funder({"wallet": "BOSS", "funder": None, "complete": True, "hub": False})
    return desk, f, state


def _funder(rep):
    return next(c for c in rep.checks if c.name == "funder_rug_link")


def test_funder_link_is_quarantine_not_a_kill(tmp_path):
    desk, f, state = _funder_filter(tmp_path)
    t = time.time()
    young = snap(mint="NEWpump", pair_created_at=t - 35 * 60)
    rep = asyncio.run(f.deep(Candidate("NEWpump"), young))
    c = _funder(rep)
    assert not c.passed and not c.permanent and "QUARANTINE" in c.detail and not rep.passed
    assert not rep.permanent_fail
    assert desk.store.rugger("DEV") is None            # a funder link alone no longer feeds the rug ledger
    assert desk.store.kv_get("filter_quarantine")["NEWpump"][3] == "funder"

    # 70 min after graduation but holders flat / first seen just now: still held
    old = snap(mint="NEWpump", pair_created_at=t - 70 * 60)
    assert not _funder(asyncio.run(f.deep(Candidate("NEWpump"), old))).passed
    f._quar["NEWpump"][0] = t - 15 * 60                # under watch for 15 min
    c = _funder(asyncio.run(f.deep(Candidate("NEWpump"), old)))
    assert not c.passed and "holders not growing" in c.detail
    state["holders"] = 180
    # liquidity fell under 90% of the first reading: held
    c = _funder(asyncio.run(f.deep(Candidate("NEWpump"), snap(mint="NEWpump", pair_created_at=t - 70 * 60,
                                                              liquidity_usd=60_000))))
    assert not c.passed and "liquidity dropping" in c.detail
    rep = asyncio.run(f.deep(Candidate("NEWpump"), old))
    c = _funder(rep)
    assert c.passed and "quarantine served" in c.detail and rep.passed
    # too early again (e.g. another reading says 40 min): the wait still rules
    assert not _funder(asyncio.run(f.deep(Candidate("NEWpump"), snap(mint="NEWpump",
                                                                    pair_created_at=t - 40 * 60)))).passed


def test_quarantine_off_restores_the_permanent_kill(tmp_path):
    desk, f, _ = _funder_filter(tmp_path)
    f.f.quarantine_minutes = 0
    rep = asyncio.run(f.deep(Candidate("NEWpump"), snap(mint="NEWpump")))
    assert _funder(rep).permanent and rep.permanent_fail and desk.store.rugger("DEV") is not None


def test_funder_only_ledger_entries_do_not_spread(tmp_path):
    desk, f, _ = _funder_filter(tmp_path)
    st = desk.store
    assert soft_ledger_reason("funder BmFdpr also funded GnvL94, in rug ledger (x)")
    assert not soft_ledger_reason("liquidity collapsed 26,556 -> 2,104 USD")
    assert not soft_ledger_reason("funded a rugging dev: liquidity collapsed")
    # DEV2 was listed by the old code only for its funder; its sibling DEV3 (same funder CHIEF) is not linked
    st.add_rugger("DEV2", "Xpump", "funder BOSS also funded OLDDEV, in rug ledger (liquidity collapsed)")
    st.save_wallet_funder({"wallet": "DEV2", "funder": "CHIEF", "complete": True, "hub": False})
    st.save_wallet_funder({"wallet": "DEV3", "funder": "CHIEF", "complete": True, "hub": False})
    st.save_wallet_funder({"wallet": "CHIEF", "funder": None, "complete": True, "hub": False})
    assert asyncio.run(f.funding.check("DEV3", skip_soft=True))[0] is None
    assert asyncio.run(f.funding.check("DEV3"))[0] is not None          # old behaviour still available
    # DEV2 itself: soft evidence (quarantine), not the permanent dev_rug_link kill
    from tests.test_rules import _rc_with_creator
    desk.rug.report = _rc_with_creator(desk.rug.report, "DEV2")
    rep = asyncio.run(f.deep(Candidate("TWOpump"), snap(mint="TWOpump")))
    assert next(c for c in rep.checks if c.name == "dev_rug_link").passed
    assert not _funder(rep).passed and not rep.permanent_fail
    # a wallet that really rugged stays a permanent kill
    desk.rug.report = _rc_with_creator(desk.rug.report, "OLDDEV")
    rep = asyncio.run(f.deep(Candidate("OLD2pump"), snap(mint="OLD2pump")))
    assert rep.permanent_fail and not next(c for c in rep.checks if c.name == "dev_rug_link").passed


def test_unproven_name_block_expires_after_graduation(tmp_path):
    desk, f = _identity_filter(tmp_path, _Probe({}))
    base = desk.rug.report
    state = {"holders": 100}

    async def report(mint):
        r = await base(mint)
        r["totalHolders"] = state["holders"]
        return r
    desk.rug.report = report
    t = time.time()

    def run(mint, minutes, **kw):
        rep = asyncio.run(f.deep(Candidate(mint), snap(mint=mint, symbol="DACITY", name="AudaCity",
                                                       pair_created_at=t - minutes * 60, **kw)))
        return next(c for c in rep.checks if c.name == "rugged_name")

    bare = dict(websites=[], socials={})
    c = run("BAREpump", 35, **bare)
    assert not c.passed and "QUARANTINE" in c.detail
    f._quar["BAREpump"][0] = t - 30 * 60
    state["holders"] = 150
    assert not run("BAREpump", 50, **bare).passed
    c = run("BAREpump", 65, **bare)
    assert c.passed and "quarantine served" in c.detail
    # channels borrowed from the rugged token never expire
    borrowed = dict(websites=[], socials={"twitter": "https://x.com/GTAVIAudaCity"})
    run("COPYpump", 35, **borrowed)
    assert "COPYpump" not in f._quar
    assert not run("COPYpump", 200, **borrowed).passed
    # expiry off: the old block for as long as the name is in the ledger
    f.f.rugged_name_expire_minutes = 0
    assert not run("BAREpump", 65, **bare).passed
