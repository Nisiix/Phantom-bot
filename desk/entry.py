"""ENTRY TIMING — when to buy a token the Analyst approved.

Buying the second a token is approved often means buying a spike: the 60-minute follow-ups show approved tokens
that first dip and then run, and others that keep falling. Instead of always buying at once, the Executor can
wait (at most [entry] window_minutes) for a better moment, with one of these rules:

  now      buy at once (the old behaviour)
  dip      buy when the price is dip_pct % under the price at approval
  bounce   wait for a dip of dip_pct %, then buy only once the price bounces bounce_pct % off the lowest point
           (no falling knives: a dip that keeps going down is never bought)
  confirm  look again after confirm_minutes: buy only if the price is >= approval price + confirm_pct %,
           otherwise drop the token
At the end of the window without the moment: fallback "skip" (no trade) or "buy" (buy at that price).
While waiting, a liquidity drop of max_liq_drop_pct % cancels the plan (a dip caused by a rug is not a bargain).

The SAME state machine (EntryPlan) runs live in the Executor (fed by fresh prices every few seconds) and offline
in the Controller (fed by the price path recorded by each 60-minute follow-up), so what the Controller measures is
exactly what the Executor does. The Controller tries different rules on the desk's own tokens and switches the
rule only when one wins on data it never saw (see desk/controller.py, agent "Timing").

Every rule still buys only with a FRESH Filter + Analyst clearance and after all the Executor's checks: waiting
never skips a safety check, it can only delay or drop a buy.
"""
from __future__ import annotations

from dataclasses import dataclass

MODES = ("now", "dip", "bounce", "confirm")
FALLBACKS = ("skip", "buy")
MAX_WINDOW_MINUTES = 15.0     # the follow-ups last 60 min and positions at most 45: a later entry can't be measured


@dataclass
class EntryPlan:
    """One approved token waiting for its moment. Prices are % moves from the approval price (raw, before costs)."""
    mode: str
    window_s: float
    dip_pct: float = 5.0
    bounce_pct: float = 2.0
    confirm_s: float = 300.0
    confirm_pct: float = 0.0
    fallback: str = "skip"
    max_liq_drop_pct: float = 20.0
    low: float = 0.0
    peak: float = 0.0
    dipped: bool = False
    state: str = "wait"           # wait | buy | skip | cancel
    why: str = ""

    def update(self, sec: float, move_pct: float, liq_ratio: float | None = None) -> str:
        """Feed one observation (seconds since approval, % price move since approval, liquidity now / at approval).
        Returns the state: "wait", "buy", "skip" (moment not found in time) or "cancel" (liquidity leaving)."""
        if self.state != "wait":
            return self.state
        if self.mode == "now":
            return self._set("buy", "acquisto immediato")
        if liq_ratio is not None and liq_ratio < 1 - self.max_liq_drop_pct / 100:
            return self._set("cancel", f"liquidità scesa del {(1 - liq_ratio) * 100:.0f}% durante l'attesa")
        if sec > self.window_s:
            if self.fallback == "buy":
                return self._set("buy", f"finestra di {self.window_s / 60:.0f} min finita: acquisto al prezzo attuale "
                                        f"({move_pct:+.1f}%)")
            return self._set("skip", f"momento non trovato in {self.window_s / 60:.0f} min")
        self.low = min(self.low, move_pct)
        self.peak = max(self.peak, move_pct)
        if self.mode == "dip":
            if move_pct <= -self.dip_pct:
                return self._set("buy", f"ribasso {move_pct:+.1f}% dal prezzo di approvazione")
        elif self.mode == "bounce":
            if self.low <= -self.dip_pct:
                self.dipped = True
            rebound = ((1 + move_pct / 100) / (1 + self.low / 100) - 1) * 100 if self.low > -100 else 0.0
            if self.dipped and rebound >= self.bounce_pct:
                return self._set("buy", f"rimbalzo +{rebound:.1f}% dal minimo {self.low:+.1f}%")
        elif self.mode == "confirm":
            if sec >= self.confirm_s:
                if move_pct >= self.confirm_pct:
                    return self._set("buy", f"conferma dopo {sec / 60:.0f} min: prezzo {move_pct:+.1f}%")
                return self._set("skip", f"dopo {sec / 60:.0f} min il prezzo è {move_pct:+.1f}% "
                                         f"(serviva >= {self.confirm_pct:+g}%)")
        return "wait"

    def _set(self, state: str, why: str) -> str:
        self.state, self.why = state, why
        return state


def plan_from(v: dict) -> EntryPlan:
    """A plan from a settings dict (the [entry] section, or a variant the Controller is testing)."""
    return EntryPlan(mode=str(v.get("mode", "now")), window_s=float(v.get("window_minutes", 10)) * 60,
                     dip_pct=float(v.get("dip_pct", 5)), bounce_pct=float(v.get("bounce_pct", 2)),
                     confirm_s=float(v.get("confirm_minutes", 5)) * 60, confirm_pct=float(v.get("confirm_pct", 0)),
                     fallback=str(v.get("fallback", "skip")), max_liq_drop_pct=float(v.get("max_liq_drop_pct", 20)))


def settings(cfg_entry) -> dict:
    """The [entry] section as the dict the Controller varies."""
    return {k: getattr(cfg_entry, k) for k in ("mode", "window_minutes", "dip_pct", "bounce_pct", "confirm_minutes",
                                               "confirm_pct", "fallback")}


def entry_point(path: list[tuple[float, float]], v: dict) -> int | None:
    """Replay a rule on a recorded path. Returns -1 = bought at approval (price move 0), i >= 0 = bought at path[i],
    None = not bought. The path has no liquidity readings: the liquidity cancel can't be replayed."""
    plan = plan_from(v)
    if plan.mode == "now":
        return -1
    for i, (sec, move) in enumerate(path):
        st = plan.update(sec, move)
        if st == "buy":
            if sec > plan.window_s:
                # fallback "buy": the window ended between two samples, the previous sample is the window's end
                return i - 1 if i > 0 else -1
            return i
        if st in ("skip", "cancel"):
            return None
    if plan.fallback == "buy" and path:
        return len(path) - 1
    return None


def rebase(path: list[tuple[float, float]], i: int) -> list[tuple[float, float]]:
    """The part of the path after an entry at path[i], as % moves from the entry price and seconds since entry."""
    if i < 0:
        return path
    t0, m0 = path[i]
    base = 1 + m0 / 100
    if base <= 0:
        return []
    return [(s - t0, ((1 + m / 100) / base - 1) * 100) for s, m in path[i + 1:]]


def entry_path(path: list[tuple[float, float]] | None, v: dict) -> list[tuple[float, float]] | None:
    """The price path the position would see with rule v (None = the rule doesn't buy this token)."""
    if not path:
        return path if plan_from(v).mode == "now" else None
    i = entry_point(path, v)
    if i is None:
        return None
    out = rebase(path, i)
    return out or None
