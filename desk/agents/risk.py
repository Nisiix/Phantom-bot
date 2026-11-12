"""RISK — owns every exit and every permission to enter.

Exits (checked every couple of seconds on executable value, i.e. after exit costs):
  * HARD STOP at -6%                       (off when [risk] loss_stop = false: user's rule 2026-11-11, no stop
                                            while the position is at break-even or at a loss, paper and live)
  * TRAILING STOP armed only after +12%;   then exit if value falls trail_distance% below the peak
  * PROFIT LOCK: after +8% net (+10% on a fast move) the stop moves to +4% net, sale fees included; from a +30%
    peak it rises with the price (70% of the peak, up to 85% when the rise is fast) and never comes down
    (desk/exits.py; levels raised by the sell buffer = configured minimum or measured exit slippage)
  * while a position is pumping Risk checks the price every second and a real sell quote every ~3 s
  * LIQUIDITY PULL: pool liquidity drops liquidity_drop_exit_pct% vs. entry -> emergency exit (0 = off: user's
    choice 2026-11-11, no exit at a loss at all)
  * DATA LOSS: no price and no sell quote for data_loss_exit_seconds -> emergency exit (0 = off: user's choice
    2026-11-11, the desk keeps holding while the feeds are silent)
  * optional time stop (forced off when loss_stop = false)
  * DEV SOLD: the creator and its linked wallets sold more than [devwatch] max_sold_pct (10%) of what they held
    from the start -> sell at once, whatever the P&L (user's rule 2026-11-11, desk/devwatch.py; on-chain balances
    read every position_check_seconds in a loop of its own)

Entry permission (Executor must ask before every buy):
  * no averaging down: never add to a held token
  * no revenge trades: never re-enter a token we lost money on (the post-loss block is per token,
    other pairs stay open); optional pause after a losing streak (off by default); daily loss limit (live only)
  * max open positions
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from ..clients.jupiter import SOL_MINT
from ..exits import LockRule, keep_share, lock_level, lock_rule, lock_step, position_lock_level, rise_speed
from ..models import ExitOrder, Position
from .base import Agent


def exit_decision(pos: Position, cfg, t: float, liquidity_usd: float | None,
                  lock: LockRule | None = None, speed: float = 0.0) -> tuple[str | None, str]:
    """Pure rule evaluation. Also arms the trailing stop and the profit lock. Returns (reason, detail).
    lock: the profit-lock rule with Risk's current sell buffer (default: from cfg).
    speed: how fast the position's peak is rising (% points per minute, exits.rise_speed), for the stop that
    rises with the price."""
    pnl = pos.pnl_pct
    stop = getattr(cfg, "exit_stop_pct", 0) or cfg.hard_stop_pct    # paper stop trial, else the hard stop
    if getattr(cfg, "loss_stop", True) and pnl <= stop:
        return "HARD_STOP", f"P&L {pnl:+.2f}% <= {stop}%"
    # trail_distance_pct = 0: no trailing stop (user 2026-11-12: only the profit stop's percentages sell)
    if cfg.trail_distance_pct > 0 and not pos.trailing_active and pos.peak_pnl_pct >= cfg.trail_activate_pct:
        pos.trailing_active = True
    if pos.trailing_active and cfg.trail_distance_pct > 0:
        trail_level = pos.peak_value_sol * (1 - cfg.trail_distance_pct / 100)
        if pos.mark_value_sol <= trail_level:
            locked = (trail_level / pos.cost_sol - 1) * 100
            return "TRAILING_STOP", (f"value fell {cfg.trail_distance_pct}% from peak "
                                     f"(peak {pos.peak_pnl_pct:+.1f}%, now {pnl:+.1f}%, trail ~{locked:+.1f}%)")
    lock = lock_rule(cfg) if lock is None else lock
    if lock.enabled and not pos.lock_armed_on:
        pos.lock_touch_s, armed = lock_step(lock, pos.peak_pnl_pct, t - pos.entry_ts, pos.lock_touch_s, 0.0,
                                               pos.rt_cost_pct)
        if armed:
            pos.lock_armed_on, pos.lock_armed_ts, pos.lock_at, pos.lock_buffer = armed, t, lock.at, lock.buffer
            pos.lock_keep = lock.keep
    if pos.lock_armed_on:
        if lock.enabled:
            # The stop rises with the price (never falls): a share of the peak, larger when the rise is fast.
            pos.lock_level = max(pos.lock_level, lock_level(lock, pos.peak_pnl_pct, speed))
            pos.lock_speed = max(pos.lock_speed, speed)
        keep = position_lock_level(pos)
        if pnl <= keep + pos.lock_buffer:
            return "PROFIT_LOCK", (f"profit lock: value back to {pnl:+.2f}% from peak {pos.peak_pnl_pct:+.1f}% "
                                   f"(keeps +{keep:.1f}% net + {pos.lock_buffer:.1f}% sale costs)")
    if cfg.liquidity_drop_exit_pct > 0 and liquidity_usd is not None and pos.entry_liquidity_usd > 0:
        drop = (1 - liquidity_usd / pos.entry_liquidity_usd) * 100
        if drop >= cfg.liquidity_drop_exit_pct:
            return "LIQUIDITY_PULL", f"pool liquidity down {drop:.0f}% since entry"
    if cfg.max_hold_minutes and (t - pos.entry_ts) / 60 >= cfg.max_hold_minutes:
        return "TIME_STOP", f"held {(t - pos.entry_ts) / 60:.0f} min"
    return None, ""


# A position whose peak rises at least this fast (% points per minute) is "hot": checked more often.
HOT_SPEED = 5.0

# Exits in the profit zone, where the sale slippage measured is comparable to a profit-lock sale (a hard stop
# during a rug gaps far below its trigger and would blow the buffer up).
STOP_EXITS = ("TRAILING_STOP", "PROFIT_LOCK")


def exit_slip(data) -> float | None:
    """Slippage of a stop exit, from a trade's data (dict or JSON): marked % at the trigger minus realized %."""
    if isinstance(data, str):
        try:
            import json
            data = json.loads(data or "{}")
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    ex = data.get("exit") or {}
    if ex.get("kind") not in STOP_EXITS or ex.get("trigger_pnl_pct") is None or ex.get("realized_pnl_pct") is None:
        return None
    return max(0.0, float(ex["trigger_pnl_pct"]) - float(ex["realized_pnl_pct"]))


class Risk(Agent):
    name = "Risk"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.r = self.cfg.risk
        s = desk.store.kv_get("risk_state", {}) or {}
        self.last_loss_ts: float = s.get("last_loss_ts", 0.0)
        self.consecutive_losses: int = s.get("consecutive_losses", 0)
        self.pause_until: float = s.get("pause_until", 0.0) if self.r.streak_pause_minutes > 0 else 0.0
        self.day: str = s.get("day", "")
        self.day_start_equity: float = s.get("day_start_equity", 0.0)
        self.day_halted: bool = s.get("day_halted", False) and self.cfg.is_live
        self.history: dict[str, dict] = s.get("history", {})   # mint -> {ts, pnl_sol}
        # Trades archived by a paper reset are still part of the per-token memory (no revenge trades).
        for t in desk.store.archived_trades() if hasattr(desk.store, "archived_trades") else []:
            self.history.setdefault(t["mint"], {"ts": t["exit_ts"] or 0.0, "pnl_sol": t["pnl_sol"] or 0.0})
        self.exiting: dict[str, float] = {}
        self.trail_announced: set[str] = set()
        self.lock_announced: set[str] = set()
        # Sale slippage measured on our stop exits (marked value at the trigger minus realized), newest last.
        self.sell_slips: list[float] = []
        for tr in desk.store.q("SELECT data FROM trades UNION ALL SELECT data FROM trades_archive") \
                if hasattr(desk.store, "q") else []:
            slip = exit_slip(tr.get("data"))
            if slip is not None:
                self.sell_slips.append(slip)
        self.sell_slips = self.sell_slips[-30:]
        self._last_lag_warn = 0.0
        self._last_quote: dict[str, float] = {}
        self._peaks: dict[str, list] = {}     # mint -> (seconds since the buy, peak %) samples: speed of the rise
        self._speed: dict[str, float] = {}    # mint -> % points per minute the peak is rising now
        self._hot = False                     # a position is pumping or has a stop armed: check faster
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
        if getattr(self.desk, "restart_pending", False):
            return False, "restart pending: no new buys until the desk is back"
        if mint in pf.positions:
            return False, "no averaging down: position already open"
        if mint in self.desk.in_flight and not own_order:
            return False, "order already in flight"
        h = self.history.get(mint)
        if h and h.get("pnl_sol", 0) < 0:
            return False, "no revenge trades: we already lost on this token"
        if h and t - h.get("ts", 0) < 6 * 3600:
            return False, "traded this token in the last 6h"
        if self.day_halted and self.cfg.is_live:
            return False, f"daily loss limit hit (-{self.r.daily_loss_limit_pct}%), entries halted until UTC midnight"
        if t < self.pause_until:
            return False, f"losing-streak pause ({(self.pause_until - t) / 60:.0f} min left)"
        pending = len({m for m in self.desk.in_flight if m not in pf.positions}) - (1 if own_order else 0)
        if len(pf.positions) + max(0, pending) >= self.cfg.executor.max_open_positions:
            return False, f"max open positions ({self.cfg.executor.max_open_positions})"
        return True, "approved"

    def sell_buffer(self) -> float:
        """Sale costs added to every profit-lock level: the configured minimum, or the median slippage of our
        last stop exits when that is larger (so the lock still ends in profit after fees). Capped at 10%."""
        base = self.r.profit_lock_sell_buffer_pct
        if len(self.sell_slips) >= 5:
            xs = sorted(self.sell_slips)
            base = max(base, xs[len(xs) // 2])
        return min(10.0, base)

    def lock(self) -> LockRule:
        return lock_rule(self.r, self.sell_buffer())

    def record_result(self, trade: dict) -> None:
        t = time.time()
        slip = exit_slip(trade.get("data"))
        if slip is not None:
            self.sell_slips = (self.sell_slips + [slip])[-30:]
        self.history[trade["mint"]] = {"ts": t, "pnl_sol": trade["pnl_sol"]}
        if trade["pnl_sol"] < 0:
            self.last_loss_ts = t
            self.consecutive_losses += 1
            # streak_pause_minutes = 0 (default) disables the losing-streak pause.
            if self.r.streak_pause_minutes > 0 and self.consecutive_losses >= self.r.max_consecutive_losses:
                self.pause_until = t + self.r.streak_pause_minutes * 60
                self.emit("risk", f"{self.consecutive_losses} losses in a row -> pausing entries "
                                  f"{self.r.streak_pause_minutes:.0f} min")
                self.consecutive_losses = 0
            else:
                self.emit("risk", "loss recorded -> token blocked for good (no revenge trades), "
                                  "other pairs stay open")
        else:
            self.consecutive_losses = 0
        self._save()

    # ── monitoring ──
    async def run(self) -> None:
        self.status("working")
        self._rollover(force=not self.day)
        stop_txt = f"{self.r.exit_stop_pct or self.r.hard_stop_pct}%" if self.r.loss_stop else "none at a loss"
        self.emit("info", f"Risk online: stop {stop_txt}, trail after +{self.r.trail_activate_pct}% "
                          f"({self.r.trail_distance_pct}% distance), daily limit "
                          + (f"-{self.r.daily_loss_limit_pct}%" if self.cfg.is_live else "off in paper (live only)"))
        if self.desk.devwatch is not None and self.cfg.devwatch.enabled:
            await asyncio.gather(self.supervised("risk", self.loop), self.supervised("devwatch", self.dev_loop))
        else:
            await self.supervised("risk", self.loop)

    async def dev_loop(self) -> None:
        """Open positions: sell when the creator's group has sold more than max_sold_pct (desk/devwatch.py)."""
        pf, dw, c = self.desk.portfolio, self.desk.devwatch, self.cfg.devwatch
        while not self.desk.stop.is_set():
            for mint, pos in list(pf.positions.items()):
                if mint in self.exiting:
                    continue
                g = dw.group(mint, self.desk.creators.get(mint))
                if g is None:
                    continue
                funder = self.desk.funders.get(mint)
                if funder:
                    g.add(funder, "dev funder")
                try:
                    await dw.refresh(g)
                except Exception:  # noqa: BLE001 - RPC hiccup: next round
                    self.state.bump("devwatch_errors")
                    continue
                if dw.breached(g) and mint not in self.exiting and pf.positions.get(mint) is pos:
                    t = time.time()
                    self.exiting[mint] = t
                    pos.trigger_pnl_pct = pos.pnl_pct
                    pf.persist(pos)
                    detail = g.describe()
                    self.emit("exit", f"DEV_SOLD: {detail}", mint, pos.symbol, pnl_pct=pos.pnl_pct)
                    await self.desk.exits.put(ExitOrder(mint, "DEV_SOLD", detail))
            if not await self.sleep(c.position_check_seconds):
                return

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
            # Daily loss limit applies to live trading only; paper keeps trading to collect data.
            if self.cfg.is_live and self.day_start_equity > 0 and not self.day_halted:
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
            if not await self.sleep(self.poll_seconds()):
                return

    def poll_seconds(self) -> float:
        """Seconds between two price checks. While a position is pumping ("hot") Risk checks twice as often (1 s
        by default): a violent pump turns into a violent dump within seconds. The floor is the DexScreener budget
        of the positions lane (60 calls/min: one batched call for all positions)."""
        base = self.r.price_poll_seconds
        if self._hot and self.desk.portfolio.positions:
            return max(1.0, min(base, self.r.hot_poll_seconds))
        return base

    def is_hot(self, pos: Position) -> bool:
        """Pumping now: its peak gained HOT_SPEED points per minute or more over the last minute. It stays hot
        for about a minute after the last new high, which is when the dump starts."""
        return self._speed.get(pos.mint, 0.0) >= HOT_SPEED

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
        # Hot positions (pumping right now) get it every ~3 s: DexScreener lags when the price moves fast. The
        # cadence widens with the number of hot positions to stay inside Jupiter's risk lane (30/min).
        due, quoted = [], set()
        n_hot = sum(1 for _, p in items if self.is_hot(p))
        hot_every = max(3.0, 2.5 * n_hot)
        for mint, pos in items:
            near = pos.pnl_pct <= (self.r.exit_stop_pct or self.r.hard_stop_pct) + 2 or pos.trailing_active \
                or bool(pos.lock_armed_on)
            every = hot_every if self.is_hot(pos) else 6 if near else 12
            if mint not in self.exiting and t - self._last_quote.get(mint, 0) >= every:
                self._last_quote[mint] = t
                due.append(pos)
        if due:
            for pos, v in zip(due, await asyncio.gather(*(self.quote_value(p) for p in due))):
                if v is not None:
                    pos.mark_value_sol = v
                    pos.last_price_ts = t
                    quoted.add(pos.mint)
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
            lock = self.lock()
            # How fast is the peak rising? (% points per minute; the stop keeps a larger share on a fast rise)
            speed = rise_speed(self._peaks.setdefault(mint, []), t - pos.entry_ts, pos.peak_pnl_pct, lock.speed_s)
            self._speed[mint] = speed
            reason, detail = exit_decision(pos, self.r, t, liq, lock, speed)
            if reason in ("HARD_STOP", "TRAILING_STOP", "PROFIT_LOCK") and mint not in quoted:
                # The mark came from DexScreener, whose price lags / jumps between pairs (seen in paper: a
                # "-7.98%" stop sold at -2.42% three seconds after entry). Confirm on a real Jupiter sell
                # quote before selling; if Jupiter can't quote, the DexScreener stop stands (safety first).
                v = await self.quote_value(pos, lane="stop")     # its own lane: never waits behind routine quotes
                if v is not None:
                    self._last_quote[mint] = t
                    pos.mark_value_sol = v
                    pos.last_price_ts = t
                    reason, detail = exit_decision(pos, self.r, t, liq, lock, speed)
                    if reason is None:
                        self.state.bump("false_stop_avoided")
            if reason == "LIQUIDITY_PULL":
                # The batched feed can return a wrong/thin pair: confirm on the full pair list before dumping.
                try:
                    full = await self.desk.dex.snapshot(mint, lane="risk")
                except Exception:  # noqa: BLE001 - feed down: keep the batched reading
                    full = None
                if full is not None:
                    pos.last_liquidity_usd = full.liquidity_usd
                    reason, detail = exit_decision(pos, self.r, t, full.liquidity_usd, lock, speed)
            dl = self.r.data_loss_exit_seconds
            if reason is None and dl > 0 and t - pos.last_price_ts > dl:
                reason, detail = "DATA_LOSS", f"no price and no sell quote for {dl:g}s"
            partial = None
            if reason is None and self.wants_scale_out(pos):
                # Partial sale (user 2026-11-12): confirmed on a real Jupiter quote, like the stops.
                if mint not in quoted:
                    v = await self.quote_value(pos, lane="stop")
                    if v is not None:
                        self._last_quote[mint] = t
                        pos.mark_value_sol = v
                        pos.last_price_ts = t
                if self.wants_scale_out(pos):
                    partial = (f"up {pos.pnl_pct:+.1f}% net: selling {self.r.scale_out_fraction_pct:g}% of the "
                               f"position, the rest stays under the stops")
            if pos.trailing_active and mint not in self.trail_announced:
                self.trail_announced.add(mint)
                self.emit("risk", f"trailing stop ARMED at peak {pos.peak_pnl_pct:+.1f}%", mint, pos.symbol)
            if pos.lock_armed_on and mint not in self.lock_announced:
                self.lock_announced.add(mint)
                self.emit("risk", f"profit lock ARMED at {pos.peak_pnl_pct:+.1f}% (target +{pos.lock_armed_on:g}% net"
                                  f"{', fast move' if pos.lock_armed_on > self.r.profit_lock_activate_pct else ''}): "
                                  f"stop now at +{position_lock_level(pos):.1f}% net (min +{pos.lock_at:g}%"
                                  + (f"; from a +{lock.keep_from:g}% peak it rises with the price, "
                                     f"{keep_share(lock, speed) * 100:.0f}% of the peak" if lock.keep > 0 else "")
                                  + f"; {pos.lock_buffer:.1f}% for sale costs)", mint, pos.symbol)
            if reason:
                pos.trigger_pnl_pct = pos.pnl_pct
            pf.persist(pos)
            if reason:
                self.exiting[mint] = t
                self.emit("exit", f"{reason}: {detail}", mint, pos.symbol, pnl_pct=pos.pnl_pct)
                await self.desk.exits.put(ExitOrder(mint, reason, detail))
            elif partial:
                self.exiting[mint] = t
                self.emit("exit", f"SCALE_OUT: {partial}", mint, pos.symbol, pnl_pct=pos.pnl_pct)
                await self.desk.exits.put(ExitOrder(mint, "SCALE_OUT", partial,
                                                    fraction=self.r.scale_out_fraction_pct / 100))
        self._hot = any(self.is_hot(p) for m, p in pf.positions.items() if m not in self.exiting)

    def wants_scale_out(self, pos: Position) -> bool:
        r = self.r
        return (not pos.scaled_out and r.scale_out_at_pct > 0 and r.scale_out_fraction_pct > 0
                and pos.pnl_pct >= r.scale_out_at_pct)

    def scale_out_done(self, mint: str) -> None:
        # The position stays open: only the sale lock is released (peaks and speed history are kept).
        self.exiting.pop(mint, None)

    async def quote_value(self, pos: Position, lane: str = "risk") -> float | None:
        raw = int(pos.tokens * 10 ** pos.decimals)
        try:
            q = await self.desk.jup.quote(pos.mint, SOL_MINT, raw, self.cfg.executor.slippage_bps, lane=lane)
        except Exception:  # noqa: BLE001
            return None
        if not q:
            return None
        return int(q["outAmount"]) / 1e9

    def exit_done(self, mint: str) -> None:
        self.exiting.pop(mint, None)
        self._last_quote.pop(mint, None)
        self._peaks.pop(mint, None)
        self._speed.pop(mint, None)

    def exit_failed(self, mint: str) -> None:
        # Allow Risk to re-issue the exit on the next tick.
        self.exiting.pop(mint, None)
