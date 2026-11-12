"""Web dashboard: state is JSON-serialisable, localhost-only, read-only."""
from __future__ import annotations

import asyncio
import json

from desk.agents.logger import Logger
from desk.agents.risk import Risk
from desk.web import Dashboard
from tests.test_rules import make_desk, position


def make_dash(tmp_path):
    desk = make_desk(tmp_path)
    desk.portfolio.open(position())
    return Dashboard(desk, Risk(desk), Logger(desk))


def test_state_serialises(tmp_path):
    s = json.loads(make_dash(tmp_path).state_json())
    assert s["mode"] == "paper" and s["balance"]["open"] == 1 and len(s["agents"]) == 8
    assert s["rules"]["max_pos_pct"] <= 8 and s["positions"][0]["symbol"] == "TST"


def _request(dash, raw: bytes) -> bytes:
    async def go():
        server = await asyncio.start_server(dash.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(raw.replace(b"PORT", str(port).encode()))
        await w.drain()
        data = await r.read()
        server.close()
        return data
    return asyncio.run(go())


def test_http_guards(tmp_path):
    dash = make_dash(tmp_path)
    ok = _request(dash, b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:PORT\r\n\r\n")
    assert ok.startswith(b"HTTP/1.1 200") and b'"mode"' in ok
    assert _request(dash, b"GET / HTTP/1.1\r\nHost: localhost:PORT\r\n\r\n").startswith(b"HTTP/1.1 200")
    assert _request(dash, b"GET /api/state HTTP/1.1\r\nHost: evil.example\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _request(dash, b"POST /api/state HTTP/1.1\r\nHost: 127.0.0.1:PORT\r\n\r\n").startswith(b"HTTP/1.1 405")


def test_activity_view(tmp_path):
    dash = make_dash(tmp_path)
    from desk.models import Event
    dash.desk.hub.emit(Event("Analyst", "score", "score 0.71", "MINT1", "TST",
                             {"score": 0.71, "breakdown": {"liq": float("nan")}, "why": ["liquidity ok"], "x": 1}))
    a = json.loads(json.dumps(dash.activity(), allow_nan=False))
    ev = a["events"][-1]
    assert ev["agent"] == "Analyst" and ev["data"]["why"] == ["liquidity ok"] and "x" not in ev["data"]
    assert ev["data"]["breakdown"]["liq"] is None and len(a["agents"]) == 8
    page = _request(dash, b"GET / HTTP/1.1\r\nHost: 127.0.0.1:PORT\r\n\r\n")
    assert page.startswith(b"HTTP/1.1 200") and b'id="vista3d"' in page and b"three@" in page
    api = _request(dash, b"GET /api/activity HTTP/1.1\r\nHost: 127.0.0.1:PORT\r\n\r\n")
    assert api.startswith(b"HTTP/1.1 200") and b'"events"' in api


def test_funnel_survives_restart_and_paper_reset(tmp_path):
    dash = make_dash(tmp_path)
    st = dash.desk.store
    st.kv_set("paper_reset_ts", 100.0)
    st.add_decision(50.0, "executor", "buy", "OLD", "OLD", "before reset", {})        # before the reset: still counted
    for mint in ("A", "B", "C"):
        st.add_decision(200.0, "filter", "reject", mint, mint, "low volume", {})
    st.add_decision(201.0, "filter", "pass", "A", "A", "ok", {})
    st.add_decision(202.0, "analyst", "approve", "A", "A", "0.7", {})
    st.add_decision(203.0, "executor", "buy", "A", "A", "bought", {})
    s = json.loads(dash.state_json())       # agent counters are all zero (fresh start), the log isn't
    assert [f["n"] for f in s["funnel"]] == [3, 1, 1, 2]       # a paper reset never zeroes the pipeline
    assert s["funnel_since"] == 50.0 and s["filter_evals"] == 4
