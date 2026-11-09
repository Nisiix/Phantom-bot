"""Agent base class: supervised loops, status reporting, event helpers."""
from __future__ import annotations

import asyncio
import traceback
from typing import Awaitable, Callable

from ..context import Desk
from ..models import Event


class Agent:
    name = "Agent"

    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.cfg = desk.cfg
        self.hub = desk.hub
        self.state = desk.hub.agents[self.name]

    # ── reporting ──
    def emit(self, kind: str, message: str, mint: str = "", symbol: str = "", **data) -> None:
        self.hub.emit(Event(self.name, kind, message, mint, symbol, data))

    def status(self, s: str) -> None:
        self.hub.status(self.name, s)

    # ── supervision ──
    async def supervised(self, label: str, fn: Callable[[], Awaitable[None]]) -> None:
        """Run a loop forever; on crash, log the error and restart with backoff."""
        backoff = 2.0
        while not self.desk.stop.is_set():
            try:
                await fn()
                if self.desk.stop.is_set():
                    return
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - an agent must never die silently
                self.status("error")
                self.emit("error", f"{label} crashed: {e.__class__.__name__}: {e}",
                          trace=traceback.format_exc(limit=4))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def run(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def sleep(self, seconds: float) -> bool:
        """Sleep unless stopping. Returns False if the desk is shutting down."""
        try:
            await asyncio.wait_for(self.desk.stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True
