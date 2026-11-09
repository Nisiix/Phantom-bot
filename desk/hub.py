"""Event hub: every agent reports here; the Logger persists, the terminal displays."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from .models import Event

AGENTS = ("Scout", "Filter", "Analyst", "Executor", "Risk", "Logger")


@dataclass
class AgentState:
    name: str
    status: str = "starting"        # starting | idle | working | error | halted
    last: str = ""
    last_ts: float = 0.0
    counters: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    heartbeat: float = field(default_factory=time.time)

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n


class Hub:
    def __init__(self) -> None:
        self.agents = {n: AgentState(n) for n in AGENTS}
        self.feed: deque[Event] = deque(maxlen=400)
        self.log_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=20000)
        self.started = time.time()
        self.banner: list[str] = []   # persistent warnings for the terminal

    def emit(self, ev: Event) -> None:
        st = self.agents.get(ev.agent)
        if st:
            st.last = ev.message
            st.last_ts = ev.ts
            st.heartbeat = ev.ts
            if ev.kind == "error":
                st.errors += 1
        self.feed.append(ev)
        try:
            self.log_queue.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    def status(self, agent: str, status: str) -> None:
        st = self.agents[agent]
        st.status = status
        st.heartbeat = time.time()
