"""Shared desk context: config, clients, queues, and shared state with clear ownership."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .clients.dexscreener import DexScreener
from .clients.http import Http
from .clients.jupiter import Jupiter
from .clients.rugcheck import RugCheck
from .clients.solana_rpc import SolanaRPC
from .config import Config
from .hub import Hub
from .models import Candidate, ClearanceAuthority, ExitOrder, FilterPass, Verdict
from .portfolio import Portfolio
from .store import Store


@dataclass
class Desk:
    cfg: Config
    hub: Hub
    store: Store
    http: Http
    dex: DexScreener
    rug: RugCheck
    jup: Jupiter
    rpc: SolanaRPC
    portfolio: Portfolio
    authority: ClearanceAuthority
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    # Hand-off queues (the only paths between agents)
    candidates: asyncio.Queue[Candidate] = field(default_factory=lambda: asyncio.Queue(maxsize=5000))
    passes: asyncio.Queue[FilterPass] = field(default_factory=lambda: asyncio.Queue(maxsize=200))
    verdicts: asyncio.Queue[Verdict] = field(default_factory=lambda: asyncio.Queue(maxsize=200))
    exits: asyncio.Queue[ExitOrder] = field(default_factory=lambda: asyncio.Queue(maxsize=200))
    # Shared knowledge (owner in comments)
    blacklist: dict[str, str] = field(default_factory=dict)     # Filter: mint -> permanent reason
    market_seen: dict[str, tuple] = field(default_factory=dict)  # Filter: mint -> (ts, vol_h1, liq_usd)
    creators: dict[str, str] = field(default_factory=dict)       # Scout/Filter: mint -> creator wallet
    in_flight: set[str] = field(default_factory=set)             # Executor: mints being bought/sold
