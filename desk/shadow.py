"""SHADOW TRACKER — follows every evaluated token for 60 minutes, bought or skipped.

For each token the desk evaluates, it snapshots the price at decision time and then records
which barrier the price touches FIRST within the horizon, for two barrier sets:

  desk barriers  (+12% / -6%)  on net-equivalent return = price move − estimated round-trip cost
                               (the same way the desk measures its own P&L)
  model barriers (+60% / -30%) on raw price move

It also simulates the desk's own exit policy on the path (hard stop, trail after +12%) to get
the return the desk would have made. Zero risk: nothing is traded.

Uses:
  * QUANT calibration: Analyst-scored tokens (above AND below 0.6) become labelled samples,
    so the model learns from skipped trades too (weighted lower than real fills).
  * "Filter regret" in the recap: did rejected tokens pump (missed) or dump (saved)?
  * Graduation-wait audit (stage "grad45"): a token held back by the graduation wait gets a second follow-up
    that starts when the wait ends (graduation + graduation_wait_minutes), i.e. "what if we had bought at
    minute 45". decision "buy_at_wait_end" = it was still tradeable then (normal 60-min follow-up);
    decision "gone_at_wait_end" = no price / liquidity under shadow_min_liquidity_usd by then (rugged or
    dead: the wait avoided it). Measurement only, nothing is traded.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import statistics
import time
from dataclasses import dataclass, field

from .quant import LABELS

MODEL_UP, MODEL_DOWN = 60.0, -30.0


@dataclass
class Shadow:
    key: str
    mint: str
    symbol: str
    stage: str               # filter | analyst
    decision: str            # reject | kill | approve | reject_score
    reason: str
    score: float | None
    ref_price: float
    ref_liq: float
    cost_pct: float
    start: float
    p: dict = field(default_factory=dict)
    source: str = ""
    features: dict | None = None   # what the desk saw at decision time (for desk/research.py)
    traded: bool = False
    last: float = 0.0
    max_ret: float = 0.0
    min_ret: float = 0.0
    desk_label: str = ""
    desk_hit_ts: float = 0.0
    model_label: str = ""
    model_hit_ts: float = 0.0
    sim_open: bool = True
    sim_peak: float = 0.0     # peak net return of the simulated position
    sim_trail: bool = False
    sim_ret: float | None = None
    path: list = field(default_factory=list)   # analyst stage: [(seconds since start, raw % move)] for the Controller


def update_shadow(s: Shadow, price: float, t: float, stop: float, up: float, trail_dist: float,
                  max_hold_min: float = 0, exit_stop: float | None = None) -> None:
    """Advance one shadow with a new price observation (pure logic, unit-tested).
    max_hold_min: Risk's time exit ([risk] max_hold_minutes, 0 = none), so the simulated position follows the
    same exit rules as a real one. exit_stop: the stop the simulated position exits at when it differs from
    the label barrier `stop` (paper stop trial)."""
    sim_stop = stop if exit_stop is None else exit_stop
    raw = (price / s.ref_price - 1) * 100
    net = raw - s.cost_pct
    s.last = raw
    s.max_ret = max(s.max_ret, raw)
    s.min_ret = min(s.min_ret, raw)
    if not s.desk_label:
        if net >= up:
            s.desk_label, s.desk_hit_ts = "up", t
        elif net <= stop:
            s.desk_label, s.desk_hit_ts = "down", t
    if not s.model_label:
        if raw >= MODEL_UP:
            s.model_label, s.model_hit_ts = "up", t
        elif raw <= MODEL_DOWN:
            s.model_label, s.model_hit_ts = "down", t
    if s.sim_open:
        # Simulated desk position: value multiple = 1 + net/100
        val = 1 + net / 100
        s.sim_peak = max(s.sim_peak, net)
        if net <= sim_stop or (max_hold_min and t - s.start >= max_hold_min * 60):
            s.sim_open, s.sim_ret = False, net
        else:
            if s.sim_peak >= up:
                s.sim_trail = True
            if s.sim_trail and val <= (1 + s.sim_peak / 100) * (1 - trail_dist / 100):
                s.sim_open, s.sim_ret = False, net


def finalize_shadow(s: Shadow) -> None:
    s.desk_label = s.desk_label or "neither"
    s.model_label = s.model_label or "neither"
    if s.sim_ret is None:
        s.sim_ret = s.last - s.cost_pct


class ShadowTracker:
    def __init__(self, desk, emit) -> None:
        self.desk = desk
        self.emit = emit
        self.cfg = desk.cfg
        self.horizon = desk.cfg.quant.horizon_minutes * 60
        self.active: dict[str, Shadow] = {}
        self.started: set[str] = set()
        self.max_active = 900
        self.completed = 0
        self._last_save = 0.0
        self.deferred: dict[str, dict] = {}   # mint -> graduation-wait audit due at the end of the wait
        self.restore()

    # ── survive restarts: tokens being followed are saved and resumed (learning data is never thrown away) ──
    def save(self) -> None:
        self.desk.store.kv_set("shadow_active", [dataclasses.asdict(s) for s in self.active.values()])
        self.desk.store.kv_set("shadow_deferred", list(self.deferred.values()))
        self._last_save = time.time()

    def restore(self) -> None:
        """Resume the follow-ups that were running when the desk stopped. The minutes the desk was off have no
        price samples; a follow-up whose 60 minutes ran out meanwhile is finalised with what it had."""
        try:
            saved = self.desk.store.kv_get("shadow_active", []) or []
        except Exception:  # noqa: BLE001 - a broken snapshot must never stop the desk
            saved = []
        names = {f.name for f in dataclasses.fields(Shadow)}
        t = time.time()
        for d in saved:
            try:
                s = Shadow(**{k: v for k, v in d.items() if k in names})
            except TypeError:
                continue
            s.path = [tuple(p) for p in s.path]
            self.started.add(s.key)
            if t - s.start >= self.horizon:
                finalize_shadow(s)
                self._store(s, end_ts=s.start + self.horizon)
            else:
                self.active[s.key] = s
        self.restored = len(self.active)
        try:
            for d in self.desk.store.kv_get("shadow_deferred", []) or []:
                if isinstance(d, dict) and d.get("mint") and d.get("due"):
                    self.deferred[d["mint"]] = d
        except Exception:  # noqa: BLE001
            pass

    # ── estimated round-trip cost (from real Jupiter round-trip quotes when available) ──
    def cost_estimate(self) -> float:
        samples = self.desk.quant.cost_samples[-50:]
        if len(samples) >= 5:
            return max(0.5, statistics.median(samples))
        return self.cfg.quant.default_cost_pct

    def start(self, mint: str, symbol: str, stage: str, decision: str, reason: str,
              price_usd: float, liq_usd: float, score: float | None = None, source: str = "",
              features: dict | None = None) -> None:
        if not self.cfg.quant.shadow_tracking or price_usd <= 0:
            return
        key = f"{mint}:{stage}"
        if key in self.started:
            return
        market = self.desk.market
        if market is not None and features is not None:
            if stage == "filter":
                market.note_launch(features.get("chg_h1"), features.get("vol_h1"))   # launch breadth
            features = {**features, **market.snapshot()}     # regime at decision time (measurement only)
        if stage == "filter" and (liq_usd < self.cfg.quant.shadow_min_liquidity_usd
                                  or len(self.active) >= self.max_active):
            return                       # untradeable dust, or at capacity (analyst rows always go in)
        self.started.add(key)
        if len(self.started) > 200_000:
            self.started.clear()
        p = {}
        if score is not None:
            pr, _, _ = self.desk.quant.probabilities(score)
            p = pr
        self.active[key] = Shadow(key, mint, symbol, stage, decision, reason[:200], score, price_usd, liq_usd,
                                  self.cost_estimate(), time.time(), p, source, features)

    # ── graduation-wait audit ──
    def defer_graduation(self, mint: str, symbol: str, pair_created_at: float, wait_minutes: float,
                         reason: str = "") -> None:
        """Schedule a "bought at the end of the graduation wait" follow-up for a token the wait held back."""
        if not self.cfg.quant.shadow_tracking or wait_minutes <= 0 or not pair_created_at:
            return
        if mint in self.deferred or f"{mint}:grad45" in self.started:
            return
        if len(self.deferred) >= 5000:
            return
        self.deferred[mint] = {"mint": mint, "symbol": symbol, "grad": float(pair_created_at),
                               "due": float(pair_created_at) + wait_minutes * 60, "wait": wait_minutes,
                               "reason": reason[:120]}

    def start_due(self, snaps: dict, t: float | None = None) -> int:
        """Start the graduation-wait audits that are due. A token without a price (3 ticks in a row) or with dust
        liquidity by then is stored straight away as gone_at_wait_end. Returns how many were handled."""
        t = t or time.time()
        n = 0
        for mint, d in list(self.deferred.items()):
            if t < d["due"]:
                continue
            snap = snaps.get(mint)
            if snap is None and d.get("tries", 0) < 2:
                d["tries"] = d.get("tries", 0) + 1      # a missing price can be a failed request: retry next tick
                continue
            self.deferred.pop(mint, None)
            key = f"{mint}:grad45"
            if key in self.started:
                continue
            self.started.add(key)
            mins = (t - d["grad"]) / 60
            why = f"graduation wait over ({mins:.0f} min after graduation); held back for: {d.get('reason', '')}"
            ok = snap is not None and snap.price_usd > 0 and snap.liquidity_usd >= self.cfg.quant.shadow_min_liquidity_usd
            if ok:
                self.active[key] = Shadow(key, mint, d["symbol"], "grad45", "buy_at_wait_end", why[:200], None,
                                          snap.price_usd, snap.liquidity_usd, self.cost_estimate(), t)
            else:
                liq = snap.liquidity_usd if snap else 0.0
                s = Shadow(key, mint, d["symbol"], "grad45", "gone_at_wait_end",
                           f"no tradeable market at the end of the wait (liquidity ${liq:,.0f}); {why}"[:200],
                           None, snap.price_usd if snap else 0.0, liq, self.cost_estimate(), t)
                s.desk_label = s.model_label = "gone"
                s.last = s.max_ret = s.min_ret = 0.0
                s.sim_ret = None
                self._store(s, end_ts=t)
            n += 1
        return n

    def mark_traded(self, mint: str) -> None:
        s = self.active.get(f"{mint}:analyst")
        if s:
            s.traded = True

    async def loop(self) -> None:
        r = self.cfg.risk
        while not self.desk.stop.is_set():
            t = time.time()
            due = {m for m, d in self.deferred.items() if t >= d["due"]}
            if due:
                try:
                    dsnaps = await self.desk.dex.snapshots(due, fresh=True, lane="shadow")
                    self.start_due(dsnaps or {}, t)
                except Exception:  # noqa: BLE001 - next tick retries
                    pass
            if self.active:
                t = time.time()
                try:
                    snaps = await self.desk.dex.snapshots({s.mint for s in self.active.values()}, fresh=True,
                                                          lane="shadow")
                except Exception:  # noqa: BLE001 - next tick retries
                    snaps = {}
                for key, s in list(self.active.items()):
                    snap = snaps.get(s.mint)
                    if snap and snap.price_usd > 0:
                        update_shadow(s, snap.price_usd, t, r.hard_stop_pct, r.trail_activate_pct,
                                      r.trail_distance_pct, float(r.max_hold_minutes or 0),
                                      r.exit_stop_pct or r.hard_stop_pct)
                        if s.stage == "analyst":   # the price path lets the Controller replay other exit rules
                            s.path.append((int(t - s.start), round(s.last, 2)))
                    if t - s.start >= self.horizon:
                        finalize_shadow(s)
                        self.active.pop(key, None)
                        self._store(s)
            if time.time() - self._last_save >= 60:
                self.save()
            try:
                await asyncio.wait_for(self.desk.stop.wait(), timeout=20)
                self.save()
                return
            except asyncio.TimeoutError:
                pass

    def _store(self, s: Shadow, end_ts: float | None = None) -> None:
        self.completed += 1
        row = {
            "start_ts": s.start, "end_ts": end_ts or time.time(), "mode": self.cfg.mode, "mint": s.mint, "symbol": s.symbol,
            "stage": s.stage, "decision": s.decision, "reason": s.reason, "score": s.score, "source": s.source,
            "traded": int(s.traded), "ref_price": s.ref_price, "ref_liq": s.ref_liq, "cost_pct": s.cost_pct,
            "desk_label": s.desk_label, "desk_hit_s": (s.desk_hit_ts - s.start) if s.desk_hit_ts else None,
            "model_label": s.model_label, "model_hit_s": (s.model_hit_ts - s.start) if s.model_hit_ts else None,
            "ret_end_pct": s.last, "max_pct": s.max_ret, "min_pct": s.min_ret, "sim_net_pct": s.sim_ret,
            "p_up": s.p.get("up"), "p_down": s.p.get("down"), "p_neither": s.p.get("neither"),
            "features": json.dumps(s.features, default=str) if s.features else None,
            "path": json.dumps(s.path, separators=(",", ":")) if s.path else None,
        }
        self.desk.store.add_shadow(row)
        # Analyst-scored, not actually traded -> a learning sample for QUANT (the real trade
        # outcome is recorded separately when a token is bought).
        if s.stage == "analyst" and s.score is not None and not s.traded:
            self.desk.quant.add(shadow_to_outcome(row, self.cfg.quant.shadow_weight))
        if s.model_label == "up" and s.decision in ("reject", "kill", "reject_score"):
            self.emit("info", f"shadow: skipped token hit +60% first (rejected for: {s.reason[:80]})",
                      s.mint, s.symbol)


def shadow_to_outcome(row: dict, weight: float) -> dict:
    p = {lb: row.get(f"p_{lb}") for lb in LABELS}
    return {
        "ts": row["end_ts"], "mode": row["mode"], "mint": row["mint"], "symbol": row["symbol"],
        "score": row["score"], "label": row["desk_label"], "ret_net_pct": row["sim_net_pct"],
        "rt_cost_pct": row["cost_pct"], "gross_pct": row["sim_net_pct"] + row["cost_pct"],
        "p_up": p["up"], "p_down": p["down"], "p_neither": p["neither"], "ev_pct": None,
        "weight": weight, "source": "shadow",
    }
