"""Wires the six agents together and runs the desk until stopped."""
from __future__ import annotations

import asyncio
import signal
import sys

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
from .quant import QuantModel
from .shadow import shadow_to_outcome
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
    # QUANT learns from paper AND live outcomes (paper fills are priced on real quotes).
    q = cfg.quant
    desk.quant = QuantModel(min_samples_to_enforce=q.min_samples_to_enforce, min_ev_pct=q.min_ev_pct,
                            prior_strength=q.prior_strength)
    stores = [store] + ([Store(cfg.data_dir / "paper.db")] if cfg.is_live else [])
    rows = []
    for st in stores:
        rows += [{**o, "weight": 1.0, "source": "trade"} for o in st.outcomes()]
        rows += [shadow_to_outcome(r, q.shadow_weight) for r in st.shadow_samples()]
        desk.quant.cost_samples += [o["rt_cost_pct"] for o in st.outcomes() if o["rt_cost_pct"]]
    desk.quant.load(rows)
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


async def run_desk(cfg: Config, use_tui: bool = True) -> None:
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
