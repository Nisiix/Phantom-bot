"""Wires the six agents together and runs the desk until stopped."""
from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from pathlib import Path

from .agents.analyst import Analyst
from .agents.controller import Controller
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.logger import Logger
from .agents.risk import Risk
from .agents.scout import Scout
from .brokers.paper import PaperBroker
from .clients.dexscreener import DexScreener
from .clients.http import Http
from .clients.jupiter import Jupiter
from .clients.rugcheck import RugCheck
from .clients.solana_rpc import SolanaRPC
from .config import Config
from .context import Desk
from .hub import Hub
from .models import ClearanceAuthority
from .portfolio import Portfolio
from .quant import learning_rows, model_from_cfg
from .recap import live_gate_status
from .store import Store
from .terminal import Terminal, headless

LIVE_PHRASE = "I_ACCEPT_THE_RISK"


class LiveRefused(RuntimeError):
    pass


async def build(cfg: Config) -> tuple[Desk, object]:
    http = Http()
    store = Store(cfg.data_dir / f"{cfg.mode}.db")
    hub = Hub()
    hub.banner.extend(w for w in cfg.warnings if w.startswith("HARD LIMIT"))
    rpc = SolanaRPC(http, cfg.secrets.rpc_url)
    jup = Jupiter(http)
    broker = None
    starting = cfg.paper_starting_balance_sol
    if cfg.is_live:
        ok, msgs = live_gate_status(Store(cfg.data_dir / "paper.db"), cfg.live_gate)
        if not ok:
            raise LiveRefused("Live trading refused — paper record not proven yet: " + "; ".join(msgs)
                              + '.\nFix: set  mode = "paper"  in config.toml and start again. The desk unlocks '
                                "live once paper trading meets the [live_gate] rules (check: python run.py gate).")
        if cfg.secrets.live_confirm != LIVE_PHRASE:
            raise LiveRefused(f"Live trading refused — set LIVE_CONFIRM={LIVE_PHRASE} in .env to confirm.")
        if not cfg.secrets.wallet_private_key:
            raise LiveRefused("Live trading refused — WALLET_PRIVATE_KEY missing in .env.")
        from .brokers.live import LiveBroker
        try:
            broker = LiveBroker(rpc, jup, cfg.secrets.wallet_private_key,
                                cfg.executor.priority_fee_max_lamports, cfg.executor.fee_reserve_sol,
                                cfg.executor.execution)
        except Exception:  # noqa: BLE001 - never surface parser details of a secret
            raise LiveRefused("Live trading refused — WALLET_PRIVATE_KEY is not a valid Solana private key "
                              "(check it with: python run.py wallet).") from None
        starting = await broker.cash()
        hub.banner.append(f"LIVE wallet {broker.pubkey[:4]}…{broker.pubkey[-4:]} — real funds at risk")
    portfolio = Portfolio(store, cfg.mode, starting)
    desk = Desk(cfg=cfg, hub=hub, store=store, http=http, dex=DexScreener(http), rug=RugCheck(http), jup=jup,
                rpc=rpc, portfolio=portfolio, authority=ClearanceAuthority())
    from .devwatch import DevWatch
    desk.devwatch = DevWatch(store, rpc, cfg.devwatch)
    # QUANT learns from paper AND live outcomes (paper fills are priced on real quotes).
    q = cfg.quant
    desk.quant = model_from_cfg(cfg)
    stores = [store] + ([Store(cfg.data_dir / "paper.db")] if cfg.is_live else [])
    for st in stores:
        desk.quant.cost_samples += [o["rt_cost_pct"] for o in st.outcomes() if o["rt_cost_pct"]]
    desk.quant.load(learning_rows(stores, cfg))
    if cfg.model.enabled:
        from .model import ModelGate
        desk.model = ModelGate(store, cfg)
    if q.market_regime:
        from .market import MarketRegime
        desk.market = MarketRegime(jup, store)
    if cfg.gmgn.enabled and cfg.secrets.gmgn_api_key:
        from .clients.gmgn import Gmgn
        desk.gmgn = Gmgn(cfg.secrets.gmgn_api_key, cfg.gmgn.plan)
    if broker is None:
        broker = PaperBroker(portfolio)
    else:
        portfolio.set_cash(await broker.cash())
    return desk, broker


def _loop_exception_handler(desk: Desk):
    """Keep harmless socket noise off the console; route everything else to desk.log.

    On Windows the proactor event loop prints a full traceback
    ("Exception in callback _ProactorBasePipeTransport._call_connection_lost ... ConnectionResetError:
    [WinError 10054]") whenever the other side drops a socket: a dashboard tab closed or reloaded, an API or
    websocket server resetting the connection. The callback runs AFTER the connection is already closed and
    nothing depends on it: the bot keeps running and the feeds reconnect by themselves. Printed on top of the
    terminal UI it looks like a crash, so it is counted quietly instead.
    Any other unhandled loop error is still shown (default handler) AND written to desk.log as an error."""
    from .models import Event
    quiet = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
    seen = {"n": 0}

    def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, quiet):
            seen["n"] += 1
            if seen["n"] in (1, 10, 100) or seen["n"] % 1000 == 0:
                desk.hub.emit(Event("Desk", "info", f"connection dropped by the remote side "
                                    f"({exc.__class__.__name__}, {seen['n']} so far): harmless, bot still running"))
            return
        loop.default_exception_handler(context)
        try:
            what = f"{exc.__class__.__name__}: {exc}" if exc else ""
            desk.hub.emit(Event("Desk", "error", f"event loop error: {context.get('message', '')} {what}".strip()))
        except Exception:  # noqa: BLE001 - the handler must never raise
            pass

    return handler


# ── restart without closing the window ──
# 2026-11-11: an update needed the user to close the window and start.bat again; the desk was off for 76 seconds
# with a position open at +20%, the price fell under its stops meanwhile and it was sold at -2.4% at the next
# start. `python run.py restart` now leaves a request file; the running desk stops buying, waits until it holds no
# position (at most max_wait_min, then it goes anyway), and exits with RESTART_CODE: start.bat / start.sh start it
# again at once with the new code, a gap of a few seconds instead of minutes.
RESTART_CODE = 75
RESTART_MAX_WAIT_MIN = 20.0


def restart_flag(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "restart.flag"


def request_restart(cfg: Config, now: bool = False, max_wait_min: float = RESTART_MAX_WAIT_MIN) -> Path:
    flag = restart_flag(cfg)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(json.dumps({"ts": time.time(), "now": bool(now), "max_wait_min": float(max_wait_min)}),
                    encoding="utf-8")
    return flag


async def restart_watch(desk: Desk, flag: Path, poll: float = 2.0) -> None:
    from .models import Event

    def say(kind: str, text: str) -> None:
        desk.hub.emit(Event("Desk", kind, text))
    try:
        flag.unlink()                      # a request left from before this start is stale
    except OSError:
        pass
    seen = 0.0
    while not desk.stop.is_set():
        await asyncio.sleep(poll)
        if not flag.exists():
            if seen:                       # request withdrawn (file deleted): back to normal
                seen, desk.restart_pending = 0.0, False
                say("info", "restart request withdrawn: buying again")
            continue
        try:
            req = json.loads(flag.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            req = {}
        t = time.time()
        max_wait = float(req.get("max_wait_min") or RESTART_MAX_WAIT_MIN) * 60
        busy = len(desk.portfolio.positions) + len(desk.in_flight)
        if not seen:
            seen, desk.restart_pending = t, True
            say("info", "restart requested: no new buys" + ("" if req.get("now") or not busy else
                f"; waiting until the {busy} open position(s) close (at most {max_wait / 60:.0f} min)"))
        if req.get("now") or not busy or t - seen >= max_wait:
            try:
                flag.unlink()
            except OSError:
                pass
            say("info", f"restarting now ({busy} open position(s): they are saved and watched again in a few "
                        f"seconds)" if busy else "restarting now (no open positions)")
            desk.restart = True
            desk.stop.set()
            return


# ── sell on the owner's request (2026-11-12) ──
# `python run.py sell <mint>` leaves data/sell.flag; the running desk sells those positions through the normal exit
# path (Jupiter quote, slippage ladder, trade + Risk memory + QUANT outcome recorded), reason MANUAL.
def sell_flag(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "sell.flag"


def request_sell(cfg: Config, mints: list[str]) -> Path:
    flag = sell_flag(cfg)
    flag.parent.mkdir(parents=True, exist_ok=True)
    old = []
    try:
        old = list(json.loads(flag.read_text(encoding="utf-8") or "{}").get("mints") or [])
    except (OSError, ValueError):
        pass
    flag.write_text(json.dumps({"ts": time.time(), "mints": list(dict.fromkeys(old + mints))}), encoding="utf-8")
    return flag


async def sell_watch(desk: Desk, risk, flag: Path, poll: float = 2.0) -> None:
    from .models import Event, ExitOrder
    while not desk.stop.is_set():
        await asyncio.sleep(poll)
        if not flag.exists():
            continue
        try:
            mints = list(json.loads(flag.read_text(encoding="utf-8") or "{}").get("mints") or [])
        except (OSError, ValueError):
            mints = []
        try:
            flag.unlink()
        except OSError:
            pass
        for mint in mints:
            pos = desk.portfolio.positions.get(mint)
            if pos is None:
                desk.hub.emit(Event("Desk", "warn", f"sell request: no open position for {mint[:8]}…"))
                continue
            if mint in risk.exiting:
                continue
            risk.exiting[mint] = time.time()
            desk.hub.emit(Event("Risk", "exit", f"MANUAL: sold on the owner's request ({pos.pnl_pct:+.1f}%)",
                                mint, pos.symbol))
            await desk.exits.put(ExitOrder(mint, "MANUAL", "sold on the owner's request"))


# ── paper reset at the next start (2026-11-12) ──
# `python run.py restart --reset-paper-usd 1000` leaves data/reset_paper.pending: the desk restarts once no position
# is open and, before starting again, resets the paper account to that capital (learning data kept, run.reset_paper).
def reset_pending(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "reset_paper.pending"


async def run_desk(cfg: Config, use_tui: bool = True) -> bool:
    """Runs the desk until stopped. Returns True when it stopped to be restarted at once (restart_watch)."""
    desk, broker = await build(cfg)
    risk = Risk(desk)
    logger = Logger(desk)
    agents = [Scout(desk), Filter(desk), Analyst(desk), Executor(desk, broker, risk), risk, logger,
              Controller(desk, risk)]

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler(desk))
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, desk.stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    tasks = [asyncio.create_task(a.run(), name=a.name) for a in agents]
    if cfg.dashboard.enabled or cfg.pages.enabled:
        from .web import Dashboard
        dash = Dashboard(desk, risk, logger)
        if cfg.dashboard.enabled:
            tasks.append(asyncio.create_task(dash.run(), name="dashboard"))
        if cfg.pages.enabled:  # public read-only copy on GitHub Pages; isolated from the trading loop
            from .pages import PagesPublisher
            tasks.append(asyncio.create_task(PagesPublisher(desk, dash).run(), name="pages"))
    ui = Terminal(desk, risk, logger)
    tasks.append(asyncio.create_task(ui.run() if use_tui and sys.stdout.isatty() else headless(desk), name="ui"))
    tasks.append(asyncio.create_task(restart_watch(desk, restart_flag(cfg)), name="restart-watch"))
    tasks.append(asyncio.create_task(sell_watch(desk, risk, sell_flag(cfg)), name="sell-watch"))
    await desk.stop.wait()
    # Graceful shutdown: give the Logger a moment to flush, then cancel.
    await asyncio.sleep(1.5)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await desk.http.close()
    if desk.gmgn is not None:
        await desk.gmgn.close()
    for a in agents:
        if isinstance(a, Analyst):
            await a.web.close()
    pf = desk.portfolio
    print(f"Desk stopped. Equity {pf.equity_sol:.4f} SOL, open positions {len(pf.positions)} "
          f"(positions are persisted and resume on restart).")
    return desk.restart
