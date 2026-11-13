"""Local web dashboard: http://127.0.0.1:8080.

A tiny asyncio HTTP server, no extra dependencies. It only binds to localhost and rejects
requests whose Host header isn't localhost (DNS-rebinding guard).

Two owner actions (user 2026-11-13), the same as the terminal commands, nothing else:
  * POST /api/sell {"mint"}        sell an open position now (= `python run.py sell <mint>`, reason MANUAL)
  * POST /api/reset-paper {"usd"}  paper only, no open position: reset the paper account to that capital
                                   (= `python run.py restart --reset-paper-usd 1000`; learning data kept)
They need the per-start token the page receives from this server (header X-Desk-Token) and a local Origin, so
another website open in the browser cannot trigger them. The GitHub Pages copy has no token and no buttons.
"""
from __future__ import annotations

import asyncio
import json
import math
import secrets
import time
import webbrowser
from pathlib import Path

from .exits import position_lock_level
from .recap import build_recap, live_gate_status
from .store import Store

PAGE = Path(__file__).with_name("dashboard.html")
# Event payload fields worth showing in the 3D view's detail panel (the rest stays server-side).
DETAIL_KEYS = ("score", "breakdown", "why", "pnl_pct", "ev_pct", "reason", "checks", "failed")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _downsample(rows: list[dict], n: int) -> list[dict]:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    out[-1] = rows[-1]
    return out


def _clean(v, depth: int = 0):
    """JSON-safe copy: NaN/inf become null, odd objects become strings, nesting is capped."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if depth > 3:
        return str(v)
    if isinstance(v, dict):
        return {str(k): _clean(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, (list, tuple)):
        return [_clean(x, depth + 1) for x in list(v)[:40]]
    return str(v)


def _detail(data) -> dict:
    if not isinstance(data, dict):
        return {}
    return {k: _clean(data[k]) for k in DETAIL_KEYS if k in data}


class Dashboard:
    def __init__(self, desk, risk, logger) -> None:
        self.desk = desk
        self.risk = risk
        self.logger = logger
        self.cfg = desk.cfg.dashboard
        self.port = self.cfg.port
        self._cache: tuple[float, bytes] = (0.0, b"")
        self._funnel: tuple[float, dict, float] = (0.0, {}, 0.0)
        self.token = secrets.token_urlsafe(24)    # owner actions only from the page this server served

    # ── state snapshot ──
    def state(self) -> dict:
        d, pf, r = self.desk, self.desk.portfolio, self.risk
        t = time.time()
        cd_left = 0.0   # post-loss block is per token (see Risk.approve_entry), no global cooldown
        agents = [{"name": n, "status": s.status, "last": s.last, "last_ts": s.last_ts,
                   "counters": s.counters, "errors": s.errors} for n, s in d.hub.agents.items()]
        feed = [{"ts": e.ts, "agent": e.agent, "kind": e.kind, "symbol": e.symbol, "mint": e.mint,
                 "message": e.message} for e in list(d.hub.feed)
                if not (e.kind == "reject" and not e.data.get("deep", True))][-150:]
        positions = [{
            "symbol": p.symbol, "mint": p.mint, "cost": p.cost_sol, "value": p.mark_value_sol,
            "pnl_pct": p.pnl_pct, "peak_pct": p.peak_pnl_pct, "trailing": p.trailing_active,
            "stop_pct": max(((p.peak_value_sol * (1 - d.cfg.risk.trail_distance_pct / 100)) / p.cost_sol - 1) * 100
                            if p.trailing_active and p.cost_sol and d.cfg.risk.trail_distance_pct > 0 else (d.cfg.risk.exit_stop_pct or d.cfg.risk.hard_stop_pct),
                            (position_lock_level(p) + p.lock_buffer) if p.lock_armed_on else -100.0)
            if d.cfg.risk.loss_stop or p.trailing_active or p.lock_armed_on else None,   # None = no stop at a loss
            "profit_lock": position_lock_level(p) if p.lock_armed_on else None,
            "age_min": (t - p.entry_ts) / 60, "score": p.score, "ev_pct": p.ev_pct, "reason": p.entry_reason,
            "exiting": p.mint in (getattr(r, "exiting", None) or {}) or p.mint in d.in_flight,
        } for p in pf.positions.values()]
        trades = d.store.q("SELECT symbol,mint,entry_ts,exit_ts,cost_sol,pnl_sol,pnl_pct,peak_pnl_pct,score,"
                           "entry_reason,exit_reason FROM trades ORDER BY exit_ts DESC LIMIT 40")
        equity = _downsample(d.store.equity_series(t - 7 * 86400), 500)
        eq = [{"ts": e["ts"], "v": e["equity_sol"]} for e in equity] + [{"ts": t, "v": pf.equity_sol}]
        fu, since = self.funnel()
        funnel = [
            {"stage": "Token esaminati", "n": fu["seen"]},
            {"stage": "Promossi dai filtri", "n": fu["passed"]},
            {"stage": "Punteggio ≥ soglia", "n": fu["approved"]},
            {"stage": "Acquistati", "n": fu["bought"]},
        ]
        q = d.quant
        try:
            gate_ok, gate_msgs = live_gate_status(Store(d.cfg.data_dir / "paper.db"), d.cfg.live_gate) \
                if d.cfg.mode == "paper" else (True, ["live mode"])
        except Exception:  # noqa: BLE001
            gate_ok, gate_msgs = False, ["gate unavailable"]
        return {
            "now": t, "mode": d.cfg.mode, "started": d.hub.started,
            "balance": {"cash": pf.cash_sol, "equity": pf.equity_sol, "start": pf.starting_cash,
                        "pnl": pf.total_pnl_sol, "sol_usd": pf.sol_usd,
                        "day_start": r.day_start_equity, "open": len(pf.positions),
                        "max_open": d.cfg.executor.max_open_positions},
            "gates": {"day_halted": r.day_halted, "pause_left": max(0.0, r.pause_until - t),
                      "cooldown_left": cd_left},
            "rules": {"max_pos_pct": d.cfg.executor.max_position_pct, "stop": (d.cfg.risk.exit_stop_pct or d.cfg.risk.hard_stop_pct) if d.cfg.risk.loss_stop else None,
                      "daily_limit": d.cfg.risk.daily_loss_limit_pct if d.cfg.is_live else 0.0,  # live only
                      "trail": d.cfg.risk.trail_activate_pct, "min_score": d.cfg.analyst.min_score},
            "quant": {"status": q.status(), "enforcing": q.enforcing, "samples": q.effective_samples,
                      "needed": q.min_samples_to_enforce, "blocked": q.blocked,
                      "calibration": [{"bucket": x["bucket"], "n": x["n"], "trades": x["trades"],
                                       "up": x["observed"]["up"], "down": x["observed"]["down"],
                                       "neither": x["observed"]["neither"], "avg": x["avg_net_pct"]}
                                      for x in q.calibration() if x["n"]]},
            "live_gate": {"ok": gate_ok, "msgs": gate_msgs},
            "next_recap": self.logger.next_recap, "banner": list(d.hub.banner[-3:]),
            "agents": agents, "feed": feed, "positions": positions, "trades": trades,
            "equity": eq, "funnel": funnel, "funnel_since": since, "filter_evals": fu["filter_checks"],
            "controller": d.controller.summary() if d.controller is not None else None,
        }

    def funnel(self) -> tuple[dict, float]:
        """Pipeline counts over the whole decision log, so neither a restart nor a paper reset zeroes them
        (user rule 2026-11-12: a reset of the balance/account value touches only the balance, never the pipeline
        of examined / promoted / approved / bought tokens). `since` = first logged decision, for the label.
        Cached for 15 s: it's a COUNT over the log."""
        t = time.time()
        if t - self._funnel[0] > 15.0:
            st = self.desk.store
            since = 0.0
            try:
                first = st.q("SELECT MIN(ts) AS t FROM decisions")
                since = float(first[0]["t"] or 0.0) if first else 0.0
                self._funnel = (t, st.funnel_since(0.0), since)
            except Exception:  # noqa: BLE001
                self._funnel = (t, {"seen": 0, "passed": 0, "approved": 0, "bought": 0, "filter_checks": 0}, since)
        return self._funnel[1], self._funnel[2]

    def state_json(self) -> bytes:
        t = time.time()
        if t - self._cache[0] > 1.0:
            self._cache = (t, json.dumps(self.state(), default=str, allow_nan=False).encode())
        return self._cache[1]

    def activity(self, limit: int = 600) -> dict:
        """Recent agent events with their rationale, for the dashboard's 3D view."""
        d = self.desk
        live = list(d.hub.feed)
        rows = [{"ts": e.ts, "agent": e.agent, "kind": e.kind, "symbol": e.symbol, "mint": e.mint,
                 "message": e.message, "data": _detail(e.data)} for e in live]
        room = limit - len(rows)
        if room > 0:  # after a restart the in-memory feed is short: backfill from the event log
            oldest = rows[0]["ts"] if rows else time.time()
            try:
                old = d.store.q("SELECT ts,agent,kind,symbol,mint,message,data FROM events WHERE ts<? "
                                "ORDER BY ts DESC LIMIT ?", (oldest, room))
            except Exception:  # noqa: BLE001
                old = []
            back = []
            for r in reversed(old):
                try:
                    data = json.loads(r["data"] or "{}")
                except (TypeError, ValueError):
                    data = {}
                back.append({"ts": r["ts"], "agent": r["agent"], "kind": r["kind"], "symbol": r["symbol"],
                             "mint": r["mint"], "message": r["message"], "data": _detail(data)})
            rows = back + rows
        agents = [{"name": n, "status": s.status, "last": s.last, "last_ts": s.last_ts,
                   "counters": s.counters, "errors": s.errors} for n, s in d.hub.agents.items()]
        return {"now": time.time(), "mode": d.cfg.mode, "started": d.hub.started,
                "agents": agents, "events": rows[-limit:]}

    def recap_text(self) -> bytes:
        d = self.desk
        t1 = time.time()
        t0 = t1 - d.cfg.recap.interval_hours * 3600
        return build_recap(d.store, d.portfolio, d.cfg.mode, t0, t1, d.hub.agents, d.quant).encode()

    # ── http ──
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = (lines[0].split(" ") + ["", "", ""])[:3]
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            host = headers.get("host", "").rsplit(":", 1)[0]
            if host not in LOCAL_HOSTS:
                return await self._send(writer, 403, b"forbidden", "text/plain")
            path = path.split("?", 1)[0]
            if method == "POST":
                return await self._action(reader, writer, path, headers)
            if method != "GET":
                return await self._send(writer, 405, b"method not allowed", "text/plain")
            if path == "/":
                body = PAGE.read_text(encoding="utf-8").replace(
                    "<!--PAGES-->", f'<script>window.DESK_TOKEN = "{self.token}";</script>', 1).encode()
                return await self._send(writer, 200, body, "text/html; charset=utf-8")
            if path == "/api/activity":
                body = json.dumps(self.activity(), default=str, allow_nan=False).encode()
                return await self._send(writer, 200, body, "application/json")
            if path == "/api/state":
                return await self._send(writer, 200, self.state_json(), "application/json")
            if path == "/api/controller":
                ctl = self.desk.controller
                text = (ctl.report or ctl.write_report()) if ctl is not None else "Controllore non attivo."
                return await self._send(writer, 200, text.encode(), "text/plain; charset=utf-8")
            if path == "/api/recap":
                return await self._send(writer, 200, self.recap_text(), "text/plain; charset=utf-8")
            return await self._send(writer, 404, b"not found", "text/plain")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass
        except Exception as e:  # noqa: BLE001 - the dashboard must never take the desk down
            try:
                await self._send(writer, 500, f"error: {e.__class__.__name__}".encode(), "text/plain")
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    # ── owner actions (user 2026-11-13) ──
    async def _action(self, reader, writer, path: str, headers: dict) -> None:
        origin = headers.get("origin", "")
        if origin and origin.split("://", 1)[-1].rsplit(":", 1)[0] not in LOCAL_HOSTS:
            return await self._send(writer, 403, b"forbidden origin", "text/plain")
        if not secrets.compare_digest(headers.get("x-desk-token", ""), self.token):
            return await self._send(writer, 403, b"missing or wrong token: reload the page", "text/plain")
        n = int(headers.get("content-length") or 0)
        if n > 4096:
            return await self._send(writer, 400, b"body too large", "text/plain")
        try:
            body = json.loads((await asyncio.wait_for(reader.readexactly(n), timeout=5)).decode() or "{}") if n else {}
        except (ValueError, asyncio.IncompleteReadError):
            return await self._send(writer, 400, b"bad json", "text/plain")
        if path == "/api/sell":
            ok, msg = self.sell(str(body.get("mint") or ""))
        elif path == "/api/reset-paper":
            ok, msg = self.reset_paper(float(body.get("usd") or 1000))
        else:
            return await self._send(writer, 404, b"not found", "text/plain")
        return await self._send(writer, 200 if ok else 409, json.dumps({"ok": ok, "msg": msg}).encode(),
                                "application/json")

    def sell(self, mint: str) -> tuple[bool, str]:
        from .app import request_sell
        pos = self.desk.portfolio.positions.get(mint)
        if pos is None:
            return False, "nessuna posizione aperta su questo token"
        if mint in getattr(self.risk, "exiting", {}) or mint in self.desk.in_flight:
            return False, f"{pos.symbol}: vendita già in corso"
        request_sell(self.desk.cfg, [mint])
        from .models import Event
        self.desk.hub.emit(Event("Desk", "info", f"sell requested from the dashboard: {pos.symbol} "
                                                 f"({pos.pnl_pct:+.1f}%)", mint, pos.symbol))
        return True, f"vendita di {pos.symbol} richiesta: il bot la esegue entro pochi secondi"

    def reset_paper(self, usd: float) -> tuple[bool, str]:
        from .app import request_restart, reset_pending
        cfg = self.desk.cfg
        if cfg.mode != "paper":
            return False, "il reset è possibile solo per il conto paper"
        if not 10 <= usd <= 1_000_000:
            return False, "capitale non valido"
        n = len(self.desk.portfolio.positions) + len(self.desk.in_flight)
        if n:
            return False, f"ci sono {n} posizioni aperte: vendile prima, poi ripeti il reset"
        reset_pending(cfg).write_text(json.dumps({"usd": usd, "ts": time.time()}), encoding="utf-8")
        request_restart(cfg)
        from .models import Event
        self.desk.hub.emit(Event("Desk", "info", f"paper reset to ${usd:,.0f} requested from the dashboard: the desk "
                                                 "restarts in a few seconds (learning data kept)"))
        return True, (f"reset del conto paper a {usd:,.0f} $ richiesto: il bot si riavvia tra pochi secondi "
                      "(i dati di apprendimento restano)")

    async def _send(self, writer, status: int, body: bytes, ctype: str) -> None:
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
                  409: "Conflict", 500: "Error"}[status]
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
            "Referrer-Policy: no-referrer\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def run(self) -> None:
        server = None
        for port in range(self.cfg.port, self.cfg.port + 10):
            try:
                server = await asyncio.start_server(self.handle, self.cfg.host, port)
                self.port = port
                break
            except OSError:
                continue
        if server is None:
            self.desk.hub.banner.append(f"Dashboard: ports {self.cfg.port}-{self.cfg.port + 9} busy, web UI off")
            return
        url = f"http://127.0.0.1:{self.port}"
        self.desk.hub.banner.append(f"Web dashboard: {url}")
        from .models import Event
        self.desk.hub.emit(Event("Logger", "info", f"Web dashboard running at {url}"))
        if self.cfg.open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        async with server:
            await self.desk.stop.wait()
