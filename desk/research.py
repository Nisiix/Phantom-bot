"""RESEARCH — which signals actually separated winners from losers, measured on our own data.

Every token the desk evaluates is followed for 60 minutes by the Shadow Tracker (bought or not).
From now on each shadow row also stores the features the desk saw at decision time (market data,
holder data, the Analyst's component scores). This module turns those rows into a plain report:

  * baseline: how often a filter survivor hit +12% before -6% (net of costs), and the average
    result of the desk's own exit policy simulated on the real price path;
  * per signal: the rows split into thirds (low / mid / high value) with hit rates and average
    simulated return, plus a 95% interval, ranked by how much the signal separates outcomes;
  * by discovery source and by UTC time of day;
  * score thresholds: what trading only above 0.55 / 0.60 / ... would have returned.

It is descriptive, not a promise: a split that looked good on a few hundred memecoins can vanish
next week. The report marks splits whose intervals overlap as "noise". Use it to decide what to
test next in paper mode, never as a guarantee.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone

# Signals reported, with a readable label. Missing values are simply skipped.
SIGNALS = {
    "liq_usd": "liquidity (USD)",
    "vol_h1": "1h volume (USD)",
    "turnover": "1h volume / liquidity",
    "txns_h1": "transactions / h",
    "buy_ratio_h1": "buy share 1h",
    "buy_ratio_m5": "buy share 5m",
    "chg_m5": "price change 5m %",
    "chg_h1": "price change 1h %",
    "chg_h6": "price change 6h %",
    "age_min": "age (minutes)",
    "mcap": "market cap (USD)",
    "top10_pct": "top-10 holders %",
    "holders": "holder count",
    "insider_pct": "insider clusters %",
    "smart_wallets": "GMGN smart wallets",
    "score": "Analyst score",
    "c_liquidity": "score: liquidity",
    "c_turnover": "score: turnover",
    "c_buy_pressure": "score: buy pressure",
    "c_momentum": "score: momentum",
    "c_holders": "score: holders",
    "c_hygiene": "score: hygiene",
    "c_website": "score: website & socials",
    "c_maturity": "score: age",
    "c_smart_money": "score: smart money",
}


def market_features(m, source: str = "") -> dict:
    """Features of a MarketSnapshot at decision time (plain floats, JSON-safe)."""
    if m is None:
        return {}
    tot_h1 = m.buys_h1 + m.sells_h1
    tot_m5 = m.buys_m5 + m.sells_m5
    return {
        "liq_usd": round(m.liquidity_usd, 2), "vol_h1": round(m.volume_h1, 2),
        "turnover": round(m.volume_h1 / max(m.liquidity_usd, 1), 4), "txns_h1": tot_h1,
        "buy_ratio_h1": round(m.buys_h1 / tot_h1, 4) if tot_h1 else None,
        "buy_ratio_m5": round(m.buys_m5 / tot_m5, 4) if tot_m5 else None,
        "chg_m5": m.change_m5, "chg_h1": m.change_h1, "chg_h6": m.change_h6,
        "age_min": round(m.age_minutes, 1), "mcap": m.market_cap or m.fdv, "dex": m.dex_id,
        "source": source,
    }


def rug_features(rug: dict | None) -> dict:
    rug = rug or {}
    gm = rug.get("gmgn") or {}
    return {
        "top10_pct": rug.get("top10_pct"), "holders": rug.get("holders_total"),
        "insider_pct": rug.get("insider_pct"), "smart_wallets": gm.get("smart_wallets"),
    }


# ── statistics ──
def _summary(rows: list[dict]) -> dict:
    n = len(rows)
    rets = [r["sim_net_pct"] for r in rows if r.get("sim_net_pct") is not None]
    avg = statistics.fmean(rets) if rets else 0.0
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    half = 1.96 * sd / math.sqrt(len(rets)) if rets else 0.0
    return {
        "n": n,
        "p_up": sum(r["desk_label"] == "up" for r in rows) / n if n else 0.0,
        "p_down": sum(r["desk_label"] == "down" for r in rows) / n if n else 0.0,
        "p_moon": sum(r.get("model_label") == "up" for r in rows) / n if n else 0.0,
        "avg": avg, "lo": avg - half, "hi": avg + half,
    }


def _thirds(pairs: list[tuple[float, dict]]) -> list[tuple[str, list[dict]]]:
    pairs.sort(key=lambda x: x[0])
    k = len(pairs) // 3
    parts = [pairs[:k], pairs[k:2 * k], pairs[2 * k:]]
    out = []
    for name, part in zip(("low", "mid", "high"), parts):
        if part:
            out.append((f"{name} {_fmt(part[0][0])}..{_fmt(part[-1][0])}", [r for _, r in part]))
    return out


def _fmt(x: float) -> str:
    a = abs(x)
    if a >= 1e6:
        return f"{x / 1e6:.1f}M"
    if a >= 1e4:
        return f"{x / 1e3:.0f}k"
    if a >= 100:
        return f"{x:.0f}"
    return f"{x:.2f}"


def parse_rows(raw: list[dict]) -> list[dict]:
    out = []
    for r in raw:
        try:
            f = json.loads(r.get("features") or "{}")
        except ValueError:
            continue
        if not isinstance(f, dict) or not r.get("desk_label"):
            continue
        if r.get("score") is not None:
            f.setdefault("score", r["score"])
        out.append({**r, "f": f})
    return out


def signal_table(rows: list[dict], min_rows: int = 30) -> list[dict]:
    """One entry per signal: thirds with stats, and the high-minus-low spread of average return."""
    table = []
    for key, label in SIGNALS.items():
        pairs = [(float(r["f"][key]), r) for r in rows
                 if isinstance(r["f"].get(key), (int, float)) and not isinstance(r["f"].get(key), bool)]
        if len(pairs) < min_rows:
            continue
        parts = [(name, _summary(part)) for name, part in _thirds(pairs)]
        if len(parts) < 3:
            continue
        lo, hi = parts[0][1], parts[-1][1]
        spread = hi["avg"] - lo["avg"]
        noise = not (hi["lo"] > lo["hi"] or lo["lo"] > hi["hi"])
        table.append({"key": key, "label": label, "n": len(pairs), "parts": parts, "spread": spread,
                      "noise": noise})
    table.sort(key=lambda t: (t["noise"], -abs(t["spread"])))
    return table


def _line(name: str, s: dict) -> str:
    return (f"  {name:<28} n={s['n']:<5} +12% first {s['p_up']:>4.0%}  -6% first {s['p_down']:>4.0%}  "
            f"+60% {s['p_moon']:>4.0%}  avg {s['avg']:+6.2f}% [{s['lo']:+.1f}, {s['hi']:+.1f}]")


def build_research(raw_rows: list[dict], stage: str = "analyst", min_rows: int = 30) -> str:
    rows = parse_rows(raw_rows)
    lines = [f"# Signal research — {stage} stage, {len(rows)} tokens followed for 60 min", ""]
    if len(rows) < min_rows:
        lines.append(f"Not enough data yet ({len(rows)} of at least {min_rows} rows with features). "
                     "Let the desk run in paper mode; every evaluated token adds a row after 60 minutes.")
        return "\n".join(lines)
    lines += ["Outcome per token, as if bought at decision time: desk barriers +12% / -6% net of costs, "
              "'+60%' = raw price reached +60% before -30%, 'avg' = desk exit policy simulated on the real "
              "price path (net, with 95% interval).", "",
              "## Baseline", _line("all", _summary(rows)), ""]

    lines.append("## Signals, ranked by how well they separate outcomes (high third minus low third)")
    for t in signal_table(rows, min_rows):
        tag = "  (noise: intervals overlap)" if t["noise"] else ""
        lines.append(f"- {t['label']} — spread {t['spread']:+.2f} pts{tag}")
        lines += [_line(name, s) for name, s in t["parts"]]
    lines.append("")

    by_src: dict[str, list[dict]] = {}
    for r in rows:
        src = str(r["f"].get("source") or r.get("source") or "?").split(":")[0]
        by_src.setdefault(src, []).append(r)
    lines.append("## By discovery source")
    lines += [_line(k, _summary(v)) for k, v in sorted(by_src.items(), key=lambda kv: -len(kv[1]))]
    lines.append("")

    by_hour: dict[str, list[dict]] = {}
    for r in rows:
        h = datetime.fromtimestamp(r["start_ts"], timezone.utc).hour // 4 * 4
        by_hour.setdefault(f"{h:02d}-{h + 4:02d} UTC", []).append(r)
    lines.append("## By time of day")
    lines += [_line(k, _summary(v)) for k, v in sorted(by_hour.items())]
    lines.append("")

    scored = [r for r in rows if isinstance(r["f"].get("score"), (int, float))]
    if scored:
        lines.append("## Score threshold what-if (only tokens at or above the threshold)")
        for thr in (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8):
            sel = [r for r in scored if r["f"]["score"] >= thr]
            if sel:
                lines.append(_line(f"score >= {thr:.2f}", _summary(sel)))
        lines.append("")

    lines += ["Read with care: these are past memecoins, not future ones. A signal is worth acting on only if it",
              "keeps its spread over several weeks of fresh data, and any change should be paper-traded first."]
    return "\n".join(lines)
