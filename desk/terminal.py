"""Live terminal: balance, agent activity, positions, equity curve, activity feed."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .context import Desk
from .hub import AGENTS

AGENT_COLORS = {"Scout": "cyan", "Filter": "yellow", "Analyst": "magenta", "Executor": "green",
                "Risk": "red", "Logger": "blue"}
KIND_STYLE = {"trade": "bold green", "exit": "bold red", "risk": "red", "kill": "dark_orange",
              "reject": "grey62", "pass": "yellow", "score": "magenta", "error": "bold red",
              "warn": "orange3", "recap": "bold blue", "info": "white"}
STATUS_STYLE = {"working": "green", "idle": "grey62", "error": "bold red", "starting": "yellow",
                "halted": "bold red"}
BLOCKS = " ▁▂▃▄▅▆▇█"


def area_chart(values: list[float], width: int, height: int) -> list[str]:
    if len(values) < 2:
        return ["waiting for equity data…".center(width)] + [""] * (height - 1)
    if len(values) > width:
        step = len(values) / width
        values = [values[min(len(values) - 1, int(i * step))] for i in range(width)]
    lo, hi = min(values), max(values)
    span = hi - lo or max(abs(hi) * 0.001, 1e-9)
    levels = [int((v - lo) / span * (height * 8 - 1)) + 1 for v in values]
    rows = []
    for r in range(height - 1, -1, -1):
        row = ""
        for lv in levels:
            fill = lv - r * 8
            row += BLOCKS[8] if fill >= 8 else (BLOCKS[fill] if fill > 0 else " ")
        rows.append(row)
    return rows


class Terminal:
    def __init__(self, desk: Desk, risk, logger) -> None:
        self.desk = desk
        self.risk = risk
        self.logger = logger
        self.console = Console()

    # ── panels ──
    def header(self) -> Panel:
        d, pf = self.desk, self.desk.portfolio
        mode = d.cfg.mode.upper()
        usd = pf.sol_usd
        eq = pf.equity_sol
        pnl = pf.total_pnl_sol
        pnl_pct = (eq / pf.starting_cash - 1) * 100 if pf.starting_cash else 0
        day_pct = (eq / self.risk.day_start_equity - 1) * 100 if self.risk.day_start_equity else 0
        up = int(time.time() - d.hub.started)
        nxt = max(0, int(self.logger.next_recap - time.time())) if self.logger.next_recap else 0
        t = Text()
        t.append(" PHANTOM DESK ", style="bold black on bright_white")
        t.append(f"  {mode} ", style="bold black on green" if mode == "PAPER" else "bold white on red")
        t.append(f"   Balance {pf.cash_sol:,.4f} SOL", style="bold")
        if usd:
            t.append(f" (${pf.cash_sol * usd:,.0f})", style="grey62")
        t.append(f"   Equity {eq:,.4f} SOL", style="bold")
        if usd:
            t.append(f" (${eq * usd:,.0f})", style="grey62")
        t.append("   P&L ")
        t.append(f"{pnl:+.4f} SOL ({pnl_pct:+.2f}%)", style="green" if pnl >= 0 else "red")
        t.append("   Today ")
        t.append(f"{day_pct:+.2f}%", style="green" if day_pct >= 0 else "red")
        t.append(f"   Open {len(pf.positions)}/{d.cfg.executor.max_open_positions}")
        t.append(f"   SOL ${usd:,.2f}" if usd else "")
        t.append(f"   Up {up // 3600}h{(up % 3600) // 60:02d}m   Recap in {nxt // 3600}h{(nxt % 3600) // 60:02d}m",
                 style="grey62")
        gate = []
        if self.risk.day_halted:
            gate.append("DAILY LOSS HALT")
        if time.time() < self.risk.pause_until:
            gate.append(f"STREAK PAUSE {(self.risk.pause_until - time.time()) / 60:.0f}m")
        cd = self.risk.r.cooldown_after_loss_minutes * 60 - (time.time() - self.risk.last_loss_ts)
        if cd > 0:
            gate.append(f"LOSS COOLDOWN {cd / 60:.0f}m")
        body = [t]
        if gate:
            body.append(Text("  ENTRY GATE: " + " | ".join(gate), style="bold red"))
        for w in d.hub.banner[-2:]:
            body.append(Text("  " + w, style="orange3"))
        return Panel(Group(*body), border_style="bright_white", padding=(0, 0))

    def agents(self) -> Panel:
        tb = Table(expand=True, box=None, padding=(0, 1), show_edge=False)
        tb.add_column("Agent", style="bold", width=9)
        tb.add_column("State", width=8)
        tb.add_column("Counters", ratio=2, overflow="ellipsis", no_wrap=True)
        tb.add_column("Last action", ratio=3, overflow="ellipsis", no_wrap=True)
        keys = {
            "Scout": ("watching", "discovered", "sent", "dropped"),
            "Filter": ("evaluated", "passed", "rejected", "blacklisted"),
            "Analyst": ("scored", "approved", "rejected"),
            "Executor": ("buys", "sells", "wins", "losses", "declined"),
            "Risk": ("open",),
            "Logger": ("logged", "recaps"),
        }
        for name in AGENTS:
            st = self.desk.hub.agents[name]
            cnt = " ".join(f"{k[:4]}:{st.counters.get(k, 0)}" for k in keys[name])
            if st.errors:
                cnt += f" err:{st.errors}"
            tb.add_row(Text(name, style=AGENT_COLORS[name]),
                       Text(st.status, style=STATUS_STYLE.get(st.status, "white")), cnt, st.last)
        return Panel(tb, title="Agents", border_style="cyan")

    def positions(self) -> Panel:
        pf = self.desk.portfolio
        tb = Table(expand=True, box=None, padding=(0, 1))
        for c, j in (("Token", "left"), ("Cost", "right"), ("Value", "right"), ("P&L", "right"),
                     ("Peak", "right"), ("Stop", "left"), ("Age", "right")):
            tb.add_column(c, justify=j)
        if not pf.positions:
            return Panel(Align.center(Text("flat — no open positions", style="grey62")), title="Positions",
                         border_style="green")
        r = self.desk.cfg.risk
        for p in pf.positions.values():
            pnl = p.pnl_pct
            stop = (f"trail {p.peak_pnl_pct - r.trail_distance_pct * (1 + p.peak_pnl_pct / 100):+.1f}%"
                    if p.trailing_active else f"hard {r.hard_stop_pct:+.0f}%")
            tb.add_row(p.symbol[:10], f"{p.cost_sol:.3f}", f"{p.mark_value_sol:.3f}",
                       Text(f"{pnl:+.2f}%", style="green" if pnl >= 0 else "red"),
                       f"{p.peak_pnl_pct:+.1f}%", stop, f"{(time.time() - p.entry_ts) / 60:.0f}m")
        return Panel(tb, title="Positions", border_style="green")

    def equity(self, width: int, height: int) -> Panel:
        series = self.desk.store.equity_series(time.time() - 7 * 24 * 3600)
        vals = [s["equity_sol"] for s in series] + [self.desk.portfolio.equity_sol]
        rows = area_chart(vals, max(10, width - 16), max(3, height - 2))
        style = "green" if vals[-1] >= vals[0] else "red"
        lo, hi = (min(vals), max(vals)) if vals else (0, 0)
        lines = []
        for i, row in enumerate(rows):
            label = f"{hi:9.3f} " if i == 0 else (f"{lo:9.3f} " if i == len(rows) - 1 else " " * 10)
            line = Text(label, style="grey62") + Text(row, style=style)
            line.no_wrap = True
            line.overflow = "crop"
            lines.append(line)
        title = f"Equity curve (SOL) — {len(series)} pts"
        return Panel(Group(*lines), title=title, border_style=style)

    def feed(self, n: int) -> Panel:
        evs = [e for e in self.desk.hub.feed if not (e.kind == "reject" and not e.data.get("deep", True))][-n:]
        lines = []
        for e in reversed(evs):
            ts = datetime.fromtimestamp(e.ts, timezone.utc).strftime("%H:%M:%S")
            t = Text(f"{ts} ", style="grey50")
            t.append(f"{e.agent:<8} ", style=AGENT_COLORS.get(e.agent, "white"))
            if e.symbol:
                t.append(f"{e.symbol[:10]:<10} ", style="bold")
            t.append(e.message, style=KIND_STYLE.get(e.kind, "white"))
            t.no_wrap = True
            t.overflow = "ellipsis"
            lines.append(t)
        return Panel(Group(*lines) if lines else Text("starting up…"), title="Agent activity",
                     border_style="magenta")

    def trades(self) -> Panel:
        rows = self.desk.store.q("SELECT * FROM trades ORDER BY exit_ts DESC LIMIT 4")
        if not rows:
            return Panel(Text("no closed trades yet — every rejection reason is in the log", style="grey62"),
                         title="Recent trades", border_style="blue")
        lines = []
        for t in rows:
            ts = datetime.fromtimestamp(t["exit_ts"], timezone.utc).strftime("%m-%d %H:%M")
            x = Text(f"{ts} {t['symbol'][:10]:<10} ")
            x.append(f"{t['pnl_pct']:+6.2f}% {t['pnl_sol']:+.4f} SOL ", style="green" if t["pnl_sol"] > 0 else "red")
            x.append(f"exit: {t['exit_reason'][:70]}", style="grey62")
            x.no_wrap = True
            x.overflow = "ellipsis"
            lines.append(x)
        return Panel(Group(*lines), title="Recent trades", border_style="blue")

    def render(self) -> Layout:
        w, h = self.console.size
        lay = Layout()
        lay.split_column(Layout(name="head", size=5 if self.risk.day_halted or self.desk.hub.banner else 3),
                         Layout(name="body"), Layout(name="foot", size=6))
        lay["body"].split_row(Layout(name="left", ratio=5), Layout(name="right", ratio=6))
        body_h = h - 9
        eq_h = max(7, min(14, body_h // 2))
        lay["left"].split_column(Layout(name="agents", size=9), Layout(name="pos"))
        lay["right"].split_column(Layout(name="eq", size=eq_h), Layout(name="feed"))
        lay["head"].update(self.header())
        lay["agents"].update(self.agents())
        lay["pos"].update(self.positions())
        lay["eq"].update(self.equity(w - int(w * 5 / 11), eq_h))
        lay["feed"].update(self.feed(max(3, body_h - eq_h - 2)))
        lay["foot"].update(self.trades())
        return lay

    async def run(self) -> None:
        with Live(self.render(), console=self.console, screen=True, refresh_per_second=2,
                  redirect_stderr=False) as live:
            while not self.desk.stop.is_set():
                try:
                    live.update(self.render())
                except Exception as e:  # noqa: BLE001 - never let the UI kill the desk
                    self.desk.hub.banner.append(f"UI error: {e}")
                await asyncio.sleep(0.5)


async def headless(desk: Desk) -> None:
    """For servers without a TTY: stream important events to stdout."""
    printed: set[int] = set()
    last_status = 0.0
    while not desk.stop.is_set():
        for e in list(desk.hub.feed):
            if id(e) in printed:
                continue
            printed.add(id(e))
            if e.kind == "reject" and not e.data.get("deep", True):
                continue
            ts = datetime.fromtimestamp(e.ts, timezone.utc).strftime("%H:%M:%S")
            print(f"{ts} [{e.agent:<8}] {e.kind.upper():<6} {e.symbol[:10]:<10} {e.message}", flush=True)
        live_ids = {id(e) for e in desk.hub.feed}
        printed &= live_ids
        if time.time() - last_status > 60:
            last_status = time.time()
            pf = desk.portfolio
            print(f"--- equity {pf.equity_sol:.4f} SOL | cash {pf.cash_sol:.4f} | open {len(pf.positions)} | "
                  + " | ".join(f"{n}:{s.status}" for n, s in desk.hub.agents.items()), flush=True)
        await asyncio.sleep(1)
