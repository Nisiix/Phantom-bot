"""QUANT — expected value after execution costs, with self-calibrating probabilities.

Every trade is labelled by which barrier its value touched FIRST within the horizon
(default 60 min), using the desk's own rules as barriers:
    up      = +trail_activate_pct reached first  (+12%: the trailing stop arms)
    down    = hard_stop_pct reached first         (-6%: stopped out), incl. gap exits below it
    neither = neither within the horizon

For a new signal, QUANT estimates P(up), P(down), P(neither) for its score bucket and the
typical gross return of each scenario, then:
    EV_net = P(up)·R_up + P(down)·R_down + P(neither)·R_neither − round-trip cost now
The Executor only buys when EV_net > min_ev_pct. QUANT can only BLOCK a trade the other
rules allow; it can never force one or loosen any rule.

Calibration (two-level shrinkage, robust with little data):
    prior  ──(prior_strength)──▶ global estimate ──(bucket_strength)──▶ per-score-bucket estimate
With few outcomes the estimate stays near the prior; as outcomes accumulate, the data takes
over. Samples come from real fills (weight 1) and from the Shadow Tracker's 60-minute follow-up of
Analyst-scored tokens that were NOT bought (weight `shadow_weight`, default 0.5). Until `min_samples_to_enforce` labelled trades exist QUANT runs in WARM-UP: it computes
and logs EV but doesn't block.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

LABELS = ("up", "down", "neither")
# Buckets below 0.6 are never traded, but their shadow outcomes show whether the score ranks well.
BUCKETS = ((0.0, 0.50), (0.50, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.80), (0.80, 1.01))
# Conservative priors for filter survivors (gross returns in %, before costs).
PRIOR_P = {"up": 0.35, "down": 0.45, "neither": 0.20}
PRIOR_R = {"up": 12.0, "down": -6.5, "neither": 0.0}


def bucket_of(score: float) -> int:
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= score < hi:
            return i
    return len(BUCKETS) - 1


def bucket_name(i: int) -> str:
    lo, hi = BUCKETS[i]
    return f"{lo:.2f}-{min(hi, 1.0):.2f}"


def label_outcome(entry_ts: float, exit_ts: float, hit_up_ts: float, hit_down_ts: float,
                  ret_net_pct: float, horizon_s: float, down_barrier_pct: float) -> str:
    """Which barrier was touched first within the horizon."""
    end = entry_ts + horizon_s
    up = 0 < hit_up_ts <= end
    down = 0 < hit_down_ts <= end
    if up and down:
        return "up" if hit_up_ts <= hit_down_ts else "down"
    if up:
        return "up"
    if down:
        return "down"
    if exit_ts <= end and ret_net_pct <= down_barrier_pct:
        return "down"  # gapped through the stop between two marks
    return "neither"


@dataclass
class QuantView:
    score: float
    bucket: str
    p: dict[str, float]
    r: dict[str, float]
    cost_pct: float
    ev_pct: float
    samples: int
    bucket_samples: int
    enforcing: bool
    passes: bool
    reason: str = ""

    def short(self) -> str:
        return (f"EV {self.ev_pct:+.2f}% (P↑{self.p['up']:.0%} P↓{self.p['down']:.0%} "
                f"P→{self.p['neither']:.0%}, cost {self.cost_pct:.1f}%, n={self.samples}"
                f"{'' if self.enforcing else ' warm-up'})")


@dataclass
class QuantModel:
    min_samples_to_enforce: int = 30
    min_ev_pct: float = 0.0
    prior_strength: float = 10.0
    bucket_strength: float = 8.0
    outcomes: list[dict] = field(default_factory=list)
    cost_samples: list[float] = field(default_factory=list)   # measured round-trip costs (%)
    blocked: int = 0
    evaluated: int = 0
    # Estimates are recomputed only when outcomes change (the shadow tracker adds thousands of rows,
    # and every Analyst verdict / shadow start asks for probabilities).
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    # ── data ──
    def load(self, rows: list[dict]) -> None:
        self.outcomes = sorted(rows, key=lambda r: r["ts"])

    def add(self, row: dict) -> None:
        self.outcomes.append(row)

    @property
    def effective_samples(self) -> float:
        return sum(o.get("weight", 1.0) for o in self.outcomes)

    @property
    def enforcing(self) -> bool:
        return self.effective_samples >= self.min_samples_to_enforce

    # ── estimation ──
    def _estimate(self, rows: list[dict], prior_p: dict, prior_r: dict, k: float) -> tuple[dict, dict]:
        n = {lb: 0 for lb in LABELS}
        s = {lb: 0.0 for lb in LABELS}
        for r in rows:
            w = r.get("weight", 1.0)
            n[r["label"]] += w
            # Capped like the Controller (-100..+300%): shadow rows with glitched price paths (+2,000,000% in
            # 22 s) once pushed EV to +16,000% and let instant rugs through the EV gate.
            s[r["label"]] += w * max(-100.0, min(300.0, r["gross_pct"] or 0.0))
        total = sum(n.values())
        p = {lb: (k * prior_p[lb] + n[lb]) / (k + total) for lb in LABELS}
        # Mean gross return per scenario, shrunk toward the prior with 3 pseudo-observations.
        r = {lb: (3 * prior_r[lb] + s[lb]) / (3 + n[lb]) for lb in LABELS}
        return p, r

    def probabilities(self, score: float) -> tuple[dict, dict, int]:
        b = bucket_of(score)
        key = (id(self.outcomes), len(self.outcomes), self.prior_strength, self.bucket_strength)
        if self._cache.get("key") != key:
            self._cache.clear()
            self._cache["key"] = key
        if b not in self._cache:
            if "global" not in self._cache:
                self._cache["global"] = self._estimate(self.outcomes, PRIOR_P, PRIOR_R, self.prior_strength)
            gp, gr = self._cache["global"]
            rows = [o for o in self.outcomes if bucket_of(o["score"]) == b]
            p, _ = self._estimate(rows, gp, gr, self.bucket_strength)
            self._cache[b] = (p, gr, len(rows))   # payoffs are pooled globally (too noisy per bucket)
        p, gr, n = self._cache[b]
        return dict(p), dict(gr), n

    def evaluate(self, score: float, cost_pct: float) -> QuantView:
        self.evaluated += 1
        p, r, nb = self.probabilities(score)
        ev = sum(p[lb] * r[lb] for lb in LABELS) - max(0.0, cost_pct)
        enforcing = self.enforcing
        ok = ev > self.min_ev_pct or not enforcing
        v = QuantView(score, bucket_name(bucket_of(score)), p, r, cost_pct, ev, round(self.effective_samples), nb,
                      enforcing, ok)
        if not ok:
            self.blocked += 1
            v.reason = f"QUANT: negative edge after costs — {v.short()}"
        return v

    # ── reporting ──
    def calibration(self) -> list[dict]:
        out = []
        for i in range(len(BUCKETS)):
            rows = [o for o in self.outcomes if bucket_of(o["score"]) == i]
            n = len(rows)
            trades = sum(1 for o in rows if o.get("source", "trade") == "trade")
            obs = {lb: (sum(1 for o in rows if o["label"] == lb) / n if n else 0.0) for lb in LABELS}
            mid = (BUCKETS[i][0] + min(BUCKETS[i][1], 1.0)) / 2
            p_now, _, _ = self.probabilities(mid)
            avg_ret = sum(o["ret_net_pct"] for o in rows) / n if n else 0.0
            out.append({"bucket": bucket_name(i), "n": n, "trades": trades, "observed": obs,
                        "model_now": p_now, "avg_net_pct": avg_ret})
        return out

    def brier(self) -> tuple[float | None, float | None]:
        """(model Brier score, naive base-rate Brier score). Lower is better."""
        rows = [o for o in self.outcomes if all(o.get(f"p_{lb}") is not None for lb in LABELS)]
        if len(rows) < 5:
            return None, None
        n = len(rows)
        base = {lb: sum(1 for o in rows if o["label"] == lb) / n for lb in LABELS}
        model = sum(sum((o[f"p_{lb}"] - (o["label"] == lb)) ** 2 for lb in LABELS) for o in rows) / n
        naive = sum(sum((base[lb] - (o["label"] == lb)) ** 2 for lb in LABELS) for o in rows) / n
        return model, naive

    def status(self) -> str:
        n = self.effective_samples
        if not self.enforcing:
            return f"QUANT warm-up {n:.0f}/{self.min_samples_to_enforce}"
        return f"QUANT enforcing (n={n:.0f}, blocked {self.blocked})"


def outcome_row(trade: dict, pos, label: str, mode: str) -> dict:
    rt = float(getattr(pos, "rt_cost_pct", 0.0) or 0.0)
    return {
        "ts": time.time(), "mode": mode, "mint": trade["mint"], "symbol": trade["symbol"],
        "score": trade["score"], "label": label, "ret_net_pct": trade["pnl_pct"], "rt_cost_pct": rt,
        "gross_pct": trade["pnl_pct"] + rt, "p_up": pos.p_up, "p_down": pos.p_down,
        "p_neither": pos.p_neither, "ev_pct": pos.ev_pct, "weight": 1.0, "source": "trade",
    }
