"""PROFIT LOCK (stop a pareggio con profitto) — shared, pure logic.

User rule (2026-11-11): once a trade is in profit by about +5/+6% NET, move the stop to about +2/+3% NET, so the
trade still closes as a win. Every percentage is NET of all costs, sale fees included: the trade's P&L is
already computed on the SOL actually spent (buy fees included) and on the executable sale value (price impact
included); on top of that the sale itself loses a bit more (paper: 0.6% latency haircut + network/priority fee;
live: slippage while the price is falling). That extra is the "sell buffer": every level is raised by it, so a
+2.5% lock really ends at about +2.5% after the sale, not at +2.5% minus fees.

  arm   when the peak reaches  on      + buffer   (on = profit_lock_activate_pct, default +5%)
        ... or fast_on + buffer if the price got to `on` within profit_lock_fast_minutes of the buy (a fast,
        strong move: give it a bit more room before locking, default +6% within 3 min)
  exit  when the value falls to  level + buffer, level = max(lock, keep x (peak - buffer))
        lock = profit_lock_pct (default +2.5%, the floor: break-even with fees plus a small profit)
        keep = profit_lock_keep_pct of the net peak (default 50%): user's choice 2026-11-11, the lock is
        PROPORTIONAL to how far the price went, so a bigger move locks a bigger profit (peak +10% net -> sells at
        +5% net, +20% -> +10%), and it rises with the peak. keep = 0 is the plain fixed lock at `lock`, kept as a
        variant the Controller measures.

buffer = max(profit_lock_sell_buffer_pct, measured slippage of our last stop exits): Risk measures, on every
stop / lock exit, how much lower the real sale ended than the value that triggered it, and uses that if worse.

The same function drives Risk (live and paper), the shadow simulation and the Controller's offline replays,
so what the Controller measures is exactly what the bot does.
"""
from __future__ import annotations

from typing import NamedTuple


class LockRule(NamedTuple):
    on: float = 0.0          # net % profit that arms the lock (0 = rule off)
    fast_on: float = 0.0     # net % needed instead when `on` was reached quickly
    fast_s: float = 0.0      # "quickly" = within this many seconds of the buy
    at: float = 0.0          # net % profit the lock sells at
    buffer: float = 0.0      # sale cost added to every level
    keep: float = 0.0        # fraction of the net peak profit the lock keeps (0 = fixed lock at `at`)

    @property
    def enabled(self) -> bool:
        return self.on > 0 and self.at > 0


OFF = LockRule()


def lock_rule(r, buffer: float | None = None) -> LockRule:
    """LockRule from a RiskCfg (or a dict with the same keys). buffer overrides the configured sell buffer."""
    g = r.get if isinstance(r, dict) else (lambda k, d=0.0: getattr(r, k, d))
    on = float(g("profit_lock_activate_pct", 0.0) or 0.0)
    if on <= 0:
        return OFF
    return LockRule(on=on, fast_on=max(on, float(g("profit_lock_fast_activate_pct", 0.0) or 0.0)),
                    fast_s=float(g("profit_lock_fast_minutes", 0.0) or 0.0) * 60,
                    at=float(g("profit_lock_pct", 0.0) or 0.0),
                    buffer=float(buffer if buffer is not None else (g("profit_lock_sell_buffer_pct", 0.0) or 0.0)),
                    keep=float(g("profit_lock_keep_pct", 0.0) or 0.0) / 100)


def lock_step(rule: LockRule, peak: float, sec: float, touch_s: float | None, armed_on: float) \
        -> tuple[float | None, float]:
    """Advance the lock with the current net peak (%) at `sec` seconds since the buy.
    Returns (touch_s, armed_on): touch_s = when the peak first reached `on` (+buffer), armed_on = the net level
    that armed it (0 = not armed yet)."""
    if armed_on or not rule.enabled:
        return touch_s, armed_on
    if touch_s is None and peak >= rule.on + rule.buffer:
        touch_s = sec
    if touch_s is not None:
        need = rule.fast_on if (rule.fast_s > 0 and touch_s <= rule.fast_s) else rule.on
        if peak >= need + rule.buffer:
            return touch_s, need
    return touch_s, 0.0


def lock_level(rule: LockRule, peak: float) -> float:
    """Net % profit an armed lock keeps after the sale: the floor, or keep x the net peak if higher."""
    return max(rule.at, rule.keep * (peak - rule.buffer))


def lock_trigger(rule: LockRule, peak: float) -> float:
    """Marked % value at which an armed lock sells (its level plus the sale costs)."""
    return lock_level(rule, peak) + rule.buffer


def position_lock_level(pos) -> float:
    """Net % an armed position's profit lock keeps now (settings frozen when it armed, current peak)."""
    return max(pos.lock_at, pos.lock_keep * (pos.peak_pnl_pct - pos.lock_buffer))
