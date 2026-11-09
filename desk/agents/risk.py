"""RISK — owns every exit and every permission to enter.

Exits (checked every couple of seconds on executable value, i.e. after exit costs):
  * HARD STOP at -6%                       (no exceptions, no "give it a minute")
  * TRAILING STOP armed only after +12%;   then exit if value falls trail_distance% below the peak
  * LIQUIDITY PULL: pool liquidity drops liquidity_drop_exit_pct% vs. entry -> emergency exit
  * DATA LOSS: no price for 60s and no sell quote -> emergency exit
  * optional time stop

Entry permission (Executor must ask before every buy):
  * no averaging down: never add to a held token
  * no revenge trades: never re-enter a token we lost money on; global cooldown after any loss;
    longer pause after a losing streak; daily loss limit halts new entries
  * max open positions
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from ..clients.jupiter import SOL_MINT
from ..models import ExitOrder, Position
from .base import Agent


def exit_decision(pos: Position, cfg, t: float, liquidity_usd: float | None) -> tuple[str | None, str]:
    """Pure rule evaluation. Also arms the trailing stop. Returns (reason, detail)."""
    pnl = pos.pnl_pct
    if pnl <= cfg.hard_stop_pct:
        return "HARD_STOP", f"P&L {pnl:+.2f}% <= {cfg.hard_stop_pct}%"
    if not pos.trailing_active and pos.peak_pnl_pct >= cfg.trail_activate_pct:
        pos.trailing_active = True
    if pos.trailing_active:
        trail_level = pos.peak_value_sol * (1 - cfg.trail_distance_pct / 100)
        if pos.mark_value_sol <= trail_level:
            locked = (trail_level / pos.cost_sol - 1) * 100
            return "TRAILING_STOP", (f"value fell {cfg.trail_distance_pct}% from peak "
                                     f"(peak {pos.peak_pnl_pct:+.1f}%, now {pnl:+.1f}%, trail ~{locked:+.1f}%)")
    if liquidity_usd is not None and pos.entry_liquidity_usd > 0:
        drop = (1 - liquidity_usd / pos.entry_liquidity_usd) * 100
        if drop >= cfg.liquidity_drop_exit_pct:
            return "LIQUIDITY_PULL", f"pool liquidity down {drop:.0f}% since entry"
    if cfg.max_hold_minutes and (t - pos.entry_ts) / 60 >= cfg.max_hold_minutes:
        return "TIME_STOP", f"held {(t - pos.entry_ts) / 60:.0f} min"
    return None, ""


class Risk(Agent):
    name = "Risk"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.r = self.cfg.risk
        s = desk.store.kv_get("risk_state", {}) or {}
        self.last_loss_ts: float = s.get("last_loss_ts", 0.0)
        self.consecutive_losses: int = s.get("consecutive_losses", 0)
        self.pause_until: float = s.get("pause_until", 0.0)
        self.day: str = s.get("day", "")
        self.day_start_equity: float = s.get("day_start_equity", 0.0)
        self.day_halted: bool = s.get("day_halted", False)
        self.history: dict[str, dict] = s.get("history", {})   # mint -> {ts, pnl_sol}
        self.exiting: dict[str, float] = {}
        self.trail_announced: set[str] = set()
        self._last_lag_warn = 0.0
        self._last_quote: dict[str, float] = {}
        self._last_equity = 0.0
        self._last_solpx = 0.0

    def _save(self) -> None:
        self.desk.store.kv_set("risk_state", {
            "last_loss_ts": self.last_loss_ts, "consecutive_losses": self.consecutive_losses,
            "pause_until": self.pause_until, "day": self.day, "day_start_equity": self.day_start_equity,
            "day_halted": self.day_halted, "history": self.history,
        })

    # ── entry permission ──
    def approve_entry(self, mint: str, own_order: bool = False) -> tuple[bool, str]:
        """own_order=True: the Executor re-checking its own in-flight buy right before the swap."""
        t = time.time()
        pf = self.desk.portfolio
        if mint in pf.positions:
            return False, "no averaging down: position already open"
        if mint in self.desk.in_flight and not own_order:
            return False, "order already in flight"
        h = self.history.get(mint)
        if h and h.get("pnl_sol", 0) < 0:
            return False, "no revenge trades: we already lost on this token"
        if h and t - h.get("ts", 0) < 6 * 3600:
            return False, "traded this token in the last 6h"
        if self.day_halted:
            return False, f"daily loss limit hit (-{self.r.daily_loss_limit_pct}%), entries halted until UTC midnight"
        if t < self.pause_until:
            return False, f"losing-streak pause ({(self.pause_until - t) / 60:.0f} min left)"
        cd = self.r.cooldown_after_loss_minutes * 60
        if t - self.last_loss_ts < cd:
            return False, f"post-loss cooldown ({(cd - (t - self.last_loss_ts)) / 60:.0f} min left)"
        pending = len({m for m in self.desk.in_flight if m not in pf.positions}) - (1 if own_order else 0)
        if len(pf.positions) + max(0, pending) >= self.cfg.executor.max_open_positions:
            return False, f"max open positions ({self.cfg.executor.max_open_positions})"
        return True, "approved"

    def record_result(self, trade: dict) -> None:
        t = time.time()
        self.history[trade["mint"]] = {"ts": t, "pnl_sol": trade["pnl_sol"]}
        if trade["pnl_sol"] < 0:
            self.last_loss_ts = t
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.r.max_consecutive_losses:
                self.pause_until = t + self.r.streak_pause_minutes * 60
                self.emit("risk", f"{self.consecutive_losses} losses in a row -> pausing entries "
                                  f"{self.r.streak_pause_minutes:.0f} min")
                self.consecutive_losses = 0
            else:
                self.emit("risk", f"loss recorded -> cooldown {self.r.cooldown_after_loss_minutes:.0f} min "
                                  f"(no revenge trades)")
        else:
            self.consecutive_losses = 0
        self._save()

    # ── monitoring ──
    async def run(self) -> None:
        self.status("working")
        self._rollover(force=not self.day)
        self.emit("info", f"Risk online: stop {self.r.hard_stop_pct}%, trail after +{self.r.trail_activate_pct}% "
                          f"({self.r.trail_distance_pct}% distance), daily limit -{self.r.daily_loss_limit_pct}%")
        await self.supervised("risk", self.loop)

    def _rollover(self, force: bool = False) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if force or today != self.day:
            self.day = today
            self.day_start_equity = self.desk.portfolio.equity_sol
            if self.day_halted:
                self.emit("risk", "new UTC day: daily loss halt lifted")
            self.day_halted = False
            self._save()

    async def loop(self) -> None:
        pf = self.desk.portfolio
        while not self.desk.stop.is_set():
            t = time.time()
            self._rollover()
            if pf.positions:
                await self.mark_positions(t)
            if t - self._last_solpx > 60:
                self._last_solpx = t
                try:
                    # One call refreshes SOL/USD and the market regime (BTC, SOL, meme basket).
                    px = await self.desk.market.refresh() if self.desk.market is not None else 0.0
                    pf.set_sol_usd(px or await self.desk.jup.sol_usd())
                except Exception as e:  # noqa: BLE001
                    self.emit("warn", f"SOL price refresh failed: {e}")
            if self.day_start_equity > 0 and not self.day_halted:
                dd = (pf.equity_sol / self.day_start_equity - 1) * 100
                if dd <= -self.r.daily_loss_limit_pct:
                    self.day_halted = True
                    self._save()
                    self.emit("risk", f"DAILY LOSS LIMIT: equity {dd:+.1f}% today -> no new entries until UTC midnight")
            if t - self._last_equity >= 30:
                self._last_equity = t
                self.desk.store.add_equity(t, pf.cash_sol, pf.equity_sol, pf.sol_usd)
            self.state.counters["open"] = len(pf.positions)
            if not pf.positions:
                self.status("idle")
            if not await self.sleep(self.r.price_poll_seconds):
                return

    async def mark_positions(self, t: float) -> None:
        pf = self.desk.portfolio
        self.status("working")
        t0 = time.time()
        snaps = await self.desk.dex.snapshots(list(pf.positions), fresh=True, lane="risk")
        lag = time.time() - t0
        if lag > 4 and t0 - self._last_lag_warn > 60:
            self._last_lag_warn = t0
            self.emit("warn", f"price feed slow ({lag:.1f}s) — stops may fill late; consider a private RPC/API plan")
        items = list(pf.positions.items())
        for mint, pos in items:
            snap = snaps.get(mint)
            if snap:
                pos.last_liquidity_usd = snap.liquidity_usd
                px = snap.price_native or (snap.price_usd / pf.sol_usd if pf.sol_usd else 0)
                if px > 0:
                    pos.mark_value_sol = pos.tokens * px * (1 - pos.exit_cost_pct / 100)
                    pos.last_price_ts = t
        # Executable truth: a real Jupiter sell quote every ~12s (or ~6s when near a stop),
        # fetched for all due positions at once instead of one after the other.
        due = []
        for mint, pos in items:
            near = pos.pnl_pct <= self.r.hard_stop_pct + 2 or pos.trailing_active
            if mint not in self.exiting and t - self._last_quote.get(mint, 0) >= (6 if near else 12):
                self._last_quote[mint] = t
                due.append(pos)
        if due:
            for pos, v in zip(due, await asyncio.gather(*(self.quote_value(p) for p in due))):
                if v is not None:
                    pos.mark_value_sol = v
                    pos.last_price_ts = t
        for mint, pos in items:
            if pf.positions.get(mint) is not pos:
                continue                     # closed by the Executor while we were quoting
            snap = snaps.get(mint)
            liq = snap.liquidity_usd if snap else None
            if pos.mark_value_sol > pos.peak_value_sol:
                pos.peak_value_sol = pos.mark_value_sol
            # QUANT labels: remember when each barrier was first touched.
            if not pos.hit_up_ts and pos.pnl_pct >= self.r.trail_activate_pct:
                pos.hit_up_ts = t
            if not pos.hit_down_ts and pos.pnl_pct <= self.r.hard_stop_pct:
                pos.hit_down_ts = t
            if mint in self.exiting:
                continue
            reason, detail = exit_decision(pos, self.r, t, liq)
            if reason == "LIQUIDITY_PULL":
                # The batched feed can return a wrong/thin pair: confirm on the full pair list before dumping.
                try:
                    full = await self.desk.dex.snapshot(mint, lane="risk")
                except Exception:  # noqa: BLE001 - feed down: the DATA_LOSS rule covers a dead market
                    full = None
                if full is not None:
                    pos.last_liquidity_usd = full.liquidity_usd
                    reason, detail = exit_decision(pos, self.r, t, full.liquidity_usd)
            if reason is None and t - pos.last_price_ts > 60:
                reason, detail = "DATA_LOSS", "no price and no sell quote for 60s"
            if pos.trailing_active and mint not in self.trail_announced:
                self.trail_announced.add(mint)
                self.emit("risk", f"trailing stop ARMED at peak {pos.peak_pnl_pct:+.1f}%", mint, pos.symbol)
            pf.persist(pos)
            if reason:
                self.exiting[mint] = t
                self.emit("exit", f"{reason}: {detail}", mint, pos.symbol, pnl_pct=pos.pnl_pct)
                await self.desk.exits.put(ExitOrder(mint, reason, detail))

    async def quote_value(self, pos: Position) -> float | None:
        raw = int(pos.tokens * 10 ** pos.decimals)
        try:
            q = await self.desk.jup.quote(pos.mint, SOL_MINT, raw, self.cfg.executor.slippage_bps, lane="risk")
        except Exception:  # noqa: BLE001
            return None
        if not q:
            return None
        return int(q["outAmount"]) / 1e9

    def exit_done(self, mint: str) -> None:
        self.exiting.pop(mint, None)
        self._last_quote.pop(mint, None)

    def exit_failed(self, mint: str) -> None:
        # Allow Risk to re-issue the exit on the next tick.
        self.exiting.pop(mint, None)
