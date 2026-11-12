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

Second version (2026-11-11, measured with `python run.py exit-study`): arm at +8% (+10% fast), fixed +4% floor,
fee rule (arm_level), and the STOP THAT RISES WITH THE PRICE for pumps: from a +30% peak the stop keeps 70% of
the peak (+30% -> +20%, +100% -> +70%), up to 85% when the peak is rising fast (rise_speed, keep_share); the
level only goes up. See lock_level.

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
    fee_mult: float = 0.0    # also arm only once the net profit is >= fee_mult x the trade's round-trip fees
    # Stop that rises with the price (user's rule 2026-11-11, "pump"): see lock_level.
    keep_from: float = 0.0   # the keep share applies only from this net peak % (0 = from arming)
    keep_fast: float = 0.0   # share kept instead when the price is rising very fast (0 = no speed rule)
    speed_full: float = 0.0  # rise of the net peak, in points per minute, at which keep_fast is fully reached
    speed_s: float = 60.0    # the rise is measured over this many seconds
    # Wider stop on mid peaks (user's rule 2026-11-12, "a +50% the stop is +25%"): under a net peak of
    # keep_high_from the share kept is keep_low (no speed boost); from keep_high_from on, keep / keep_fast.
    keep_low: float = 0.0        # share of the net peak kept below keep_high_from (0 = band off)
    keep_high_from: float = 0.0  # net peak % from which the normal keep share applies

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
                    keep=float(g("profit_lock_keep_pct", 0.0) or 0.0) / 100,
                    fee_mult=float(g("profit_lock_fee_multiple", 0.0) or 0.0),
                    keep_from=float(g("profit_lock_keep_from_pct", 0.0) or 0.0),
                    keep_fast=float(g("profit_lock_keep_fast_pct", 0.0) or 0.0) / 100,
                    speed_full=float(g("profit_lock_speed_full", 0.0) or 0.0),
                    speed_s=float(g("profit_lock_speed_seconds", 60.0) or 60.0),
                    keep_low=float(g("profit_lock_keep_low_pct", 0.0) or 0.0) / 100,
                    keep_high_from=float(g("profit_lock_keep_high_from_pct", 0.0) or 0.0))


def arm_level(rule: LockRule, cost: float = 0.0) -> float:
    """Net % the lock needs before it can arm on a trade whose round-trip fees are `cost` %: the configured
    level, or fee_mult x the fees when that is higher (user's rule 2026-11-11: arm only once the profit has
    covered the fees, so expensive tokens get more room; cheap ones, fees < 3%, use the plain level ~+7%)."""
    return max(rule.on, rule.fee_mult * max(0.0, cost))


def lock_step(rule: LockRule, peak: float, sec: float, touch_s: float | None, armed_on: float,
              cost: float = 0.0) -> tuple[float | None, float]:
    """Advance the lock with the current net peak (%) at `sec` seconds since the buy.
    cost = the trade's round-trip fees (%), for the fee rule (arm_level).
    Returns (touch_s, armed_on): touch_s = when the peak first reached the arming level (+buffer), armed_on = the
    net level that armed it (0 = not armed yet)."""
    if armed_on or not rule.enabled:
        return touch_s, armed_on
    on = arm_level(rule, cost)
    if touch_s is None and peak >= on + rule.buffer:
        touch_s = sec
    if touch_s is not None:
        need = max(on, rule.fast_on) if (rule.fast_s > 0 and touch_s <= rule.fast_s) else on
        if peak >= need + rule.buffer:
            return touch_s, need
    return touch_s, 0.0


MIN_SPEED_S = 10.0    # no speed reading on less than this many seconds of history (one tick is not a trend)


def rise_speed(hist: list, sec: float, peak: float, window: float = 60.0) -> float:
    """How fast the trade's net peak is rising, in % points per minute over the last `window` seconds.
    hist is the caller's own list of (sec, peak) samples: this appends the new one and drops the old ones.
    A peak only goes up, so the speed is never negative; it falls back to 0 once the price stops making highs."""
    hist.append((sec, peak))
    while len(hist) > 2 and hist[1][0] <= sec - window:
        hist.pop(0)
    t0, p0 = hist[0]
    dt = sec - t0
    if dt < MIN_SPEED_S:
        return 0.0
    return max(0.0, (peak - p0) * 60.0 / dt)


def keep_share(rule: LockRule, speed: float = 0.0) -> float:
    """Share of the net peak the stop keeps: `keep`, rising towards `keep_fast` with the speed of the rise
    (user: "the faster it goes up, the more the stop must go up": a violent pump is followed by a violent dump)."""
    if rule.keep_fast > rule.keep and rule.speed_full > 0 and speed > 0:
        return rule.keep + (rule.keep_fast - rule.keep) * min(1.0, speed / rule.speed_full)
    return rule.keep


def lock_level(rule: LockRule, peak: float, speed: float = 0.0) -> float:
    """Net % profit an armed lock keeps after the sale: the floor `at`, or the keep share of the net peak if
    higher. With keep_from the share starts only at that peak (below it the swings of a young token would close
    the trade too early: measured 2026-11-11). User's figures: +30% -> stop +20%, +100% -> +70% (70% of the
    peak), more when the rise is fast. Callers keep the highest level reached (the stop never goes down)."""
    net_peak = peak - rule.buffer
    if rule.keep_low > 0 and net_peak < rule.keep_high_from:
        share = rule.keep_low        # mid peaks: a wider stop, so a token still climbing is left running
    else:
        share = keep_share(rule, speed)
    if share <= 0 or net_peak < rule.keep_from:
        return rule.at
    return max(rule.at, share * net_peak)


def lock_trigger(rule: LockRule, peak: float, speed: float = 0.0, level: float = 0.0) -> float:
    """Marked % value at which an armed lock sells: its level (never under `level`, the highest one reached so
    far) plus the sale costs."""
    return max(level, lock_level(rule, peak, speed)) + rule.buffer


def position_lock_level(pos) -> float:
    """Net % an armed position's profit lock keeps now: the floor frozen when it armed, or the highest level
    Risk has raised it to since (pos.lock_level, see agents/risk.py exit_decision)."""
    return max(pos.lock_at, getattr(pos, "lock_level", 0.0) or 0.0)
