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
"""
from __future__ import annotations

import asyncio
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


def update_shadow(s: Shadow, price: float, t: float, stop: float, up: float, trail_dist: float) -> None:
    """Advance one shadow with a new price observation (pure logic, unit-tested)."""
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
        if net <= stop:
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

    def mark_traded(self, mint: str) -> None:
        s = self.active.get(f"{mint}:analyst")
        if s:
            s.traded = True

    async def loop(self) -> None:
        r = self.cfg.risk
        while not self.desk.stop.is_set():
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
                                      r.trail_distance_pct)
                    if t - s.start >= self.horizon:
                        finalize_shadow(s)
                        self.active.pop(key, None)
                        self._store(s)
            try:
                await asyncio.wait_for(self.desk.stop.wait(), timeout=20)
                return
            except asyncio.TimeoutError:
                pass

    def _store(self, s: Shadow) -> None:
        self.completed += 1
        row = {
            "start_ts": s.start, "end_ts": time.time(), "mode": self.cfg.mode, "mint": s.mint, "symbol": s.symbol,
            "stage": s.stage, "decision": s.decision, "reason": s.reason, "score": s.score, "source": s.source,
            "traded": int(s.traded), "ref_price": s.ref_price, "ref_liq": s.ref_liq, "cost_pct": s.cost_pct,
            "desk_label": s.desk_label, "desk_hit_s": (s.desk_hit_ts - s.start) if s.desk_hit_ts else None,
            "model_label": s.model_label, "model_hit_s": (s.model_hit_ts - s.start) if s.model_hit_ts else None,
            "ret_end_pct": s.last, "max_pct": s.max_ret, "min_pct": s.min_ret, "sim_net_pct": s.sim_ret,
            "p_up": s.p.get("up"), "p_down": s.p.get("down"), "p_neither": s.p.get("neither"),
            "features": json.dumps(s.features, default=str) if s.features else None,
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
