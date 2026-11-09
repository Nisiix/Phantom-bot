"""Wires the six agents together and runs the desk until stopped."""
from __future__ import annotations

import asyncio
import signal
import sys

from .agents.analyst import Analyst
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
            raise LiveRefused("Live trading refused — paper record not proven yet: " + "; ".join(msgs))
        if cfg.secrets.live_confirm != LIVE_PHRASE:
            raise LiveRefused(f"Live trading refused — set LIVE_CONFIRM={LIVE_PHRASE} in .env to confirm.")
        if not cfg.secrets.wallet_private_key:
            raise LiveRefused("Live trading refused — WALLET_PRIVATE_KEY missing in .env.")
        from .brokers.live import LiveBroker
        broker = LiveBroker(rpc, jup, cfg.secrets.wallet_private_key,
                            cfg.executor.priority_fee_max_lamports, cfg.executor.fee_reserve_sol)
        starting = await broker.cash()
        hub.banner.append(f"LIVE wallet {broker.pubkey[:4]}…{broker.pubkey[-4:]} — real funds at risk")
    portfolio = Portfolio(store, cfg.mode, starting)
    desk = Desk(cfg=cfg, hub=hub, store=store, http=http, dex=DexScreener(http), rug=RugCheck(http), jup=jup,
                rpc=rpc, portfolio=portfolio, authority=ClearanceAuthority())
    if broker is None:
        broker = PaperBroker(portfolio)
    else:
        portfolio.set_cash(await broker.cash())
    return desk, broker


async def run_desk(cfg: Config, use_tui: bool = True) -> None:
    desk, broker = await build(cfg)
    risk = Risk(desk)
    logger = Logger(desk)
    agents = [Scout(desk), Filter(desk), Analyst(desk), Executor(desk, broker, risk), risk, logger]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, desk.stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    tasks = [asyncio.create_task(a.run(), name=a.name) for a in agents]
    ui = Terminal(desk, risk, logger)
    tasks.append(asyncio.create_task(ui.run() if use_tui and sys.stdout.isatty() else headless(desk), name="ui"))
    await desk.stop.wait()
    # Graceful shutdown: give the Logger a moment to flush, then cancel.
    await asyncio.sleep(1.5)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await desk.http.close()
    for a in agents:
        if isinstance(a, Analyst):
            await a.web.close()
    pf = desk.portfolio
    print(f"Desk stopped. Equity {pf.equity_sol:.4f} SOL, open positions {len(pf.positions)} "
          f"(positions are persisted and resume on restart).")
