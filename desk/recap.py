"""4-hour recap: performance, every trade with its reasons, rejection analytics, risk events."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .store import Store


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def sparkline(values: list[float], width: int = 48) -> str:
    if len(values) < 2:
        return "(not enough data yet)"
    step = max(1, len(values) // width)
    vals = values[::step][-width:]
    lo, hi = min(vals), max(vals)
    blocks = "▁▂▃▄▅▆▇█"
    if hi - lo < 1e-12:
        return blocks[3] * len(vals)
    return "".join(blocks[int((v - lo) / (hi - lo) * (len(blocks) - 1))] for v in vals)


def max_drawdown(values: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v / peak - 1) * 100)
    return mdd


def trade_stats(trades: list[dict]) -> dict:
    wins = [t for t in trades if t["pnl_sol"] > 0]
    losses = [t for t in trades if t["pnl_sol"] <= 0]
    gross_win = sum(t["pnl_sol"] for t in wins)
    gross_loss = -sum(t["pnl_sol"] for t in losses)
    return {
        "n": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "pnl_sol": sum(t["pnl_sol"] for t in trades),
        "avg_win_pct": sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "best": max(trades, key=lambda t: t["pnl_pct"]) if trades else None,
        "worst": min(trades, key=lambda t: t["pnl_pct"]) if trades else None,
    }


def quant_section(quant) -> list[str]:
    if quant is None:
        return []
    lines = ["", "## QUANT — edge after costs (calibration)", f"- Status: {quant.status()} | signals evaluated "
             f"{quant.evaluated}, blocked for negative EV {quant.blocked}"]
    model, naive = quant.brier()
    if model is not None:
        verdict = "beats" if model < naive else "does NOT beat"
        lines.append(f"- Forecast quality (Brier, lower = better): model {model:.3f} vs base-rate {naive:.3f} "
                     f"→ model {verdict} the naive base rate")
    lines.append("- Score bucket → observed outcome of '+12% first / -6% first / neither in 60m' "
                 "(trades + skipped tokens) | model now | avg net")
    for c in quant.calibration():
        if not c["n"]:
            continue
        o, m = c["observed"], c["model_now"]
        lines.append(f"  - {c['bucket']}: n={c['n']} ({c['trades']} real) | ↑{o['up']:.0%} ↓{o['down']:.0%} "
                     f"→{o['neither']:.0%} | model ↑{m['up']:.0%} ↓{m['down']:.0%} | {c['avg_net_pct']:+.1f}%")
    _, r, _ = quant.probabilities(0.65)
    lines.append(f"- Learned gross payoff per scenario: ↑ {r['up']:+.1f}% | ↓ {r['down']:+.1f}% | "
                 f"→ {r['neither']:+.1f}%")
    return lines


def shadow_section(store: Store, t0: float, t1: float) -> list[str]:
    rows = store.shadows_between(t0, t1)
    if not rows:
        return ["", "## Shadow tracking (60 min after every decision)", "- no completed follow-ups in this window yet"]
    lines = ["", "## Shadow tracking (60 min after every decision)",
             f"- {len(rows)} tokens followed to the end of their 60-minute window"]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(f"{r['stage']}:{r['decision']}", []).append(r)
    for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        du = sum(r["desk_label"] == "up" for r in rs) / n
        dd = sum(r["desk_label"] == "down" for r in rs) / n
        mu = sum(r["model_label"] == "up" for r in rs) / n
        md = sum(r["model_label"] == "down" for r in rs) / n
        sim = sum(r["sim_net_pct"] or 0 for r in rs) / n
        lines.append(f"  - {g} (n={n}): desk +12 first {du:.0%}, -6 first {dd:.0%} | model +60 first {mu:.0%}, "
                     f"-30 first {md:.0%} | avg simulated desk trade {sim:+.1f}%")
    missed = [r for r in rows if r["decision"] in ("reject", "kill", "reject_score") and r["model_label"] == "up"]
    saved = [r for r in rows if r["decision"] in ("reject", "kill", "reject_score") and r["model_label"] == "down"]
    lines.append(f"- Skipped tokens that hit +60% first (missed): {len(missed)} | hit -30% first (avoided): {len(saved)}")
    reasons = Counter()
    for r in missed:
        reasons[(r["reason"] or "").split(":")[0][:40]] += 1
    if reasons:
        lines.append("- Missed winners were rejected for: " + ", ".join(f"{k} ×{v}" for k, v in reasons.most_common(5)))
    for r in sorted(missed, key=lambda r: -r["max_pct"])[:5]:
        lines.append(f"  - {r['symbol']}: max {r['max_pct']:+.0f}% / min {r['min_pct']:+.0f}% — {r['reason'][:110]}")
    return lines


def build_recap(store: Store, portfolio, mode: str, t0: float, t1: float, agents: dict | None = None,
                quant=None) -> str:
    trades = store.trades_between(t0, t1)
    all_trades = store.all_trades()
    eq = store.equity_series(t0)
    eq_vals = [e["equity_sol"] for e in eq]
    eq_all = [e["equity_sol"] for e in store.equity_series(0)]
    decisions = store.decisions_between(t0, t1)
    risk_events = store.events_between(t0, t1, ("risk", "exit", "error", "kill"))
    s = trade_stats(trades)
    sa = trade_stats(all_trades)
    start_eq = eq_vals[0] if eq_vals else portfolio.equity_sol
    end_eq = portfolio.equity_sol
    usd = portfolio.sol_usd
    pf_txt = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"

    def sol(x: float) -> str:
        return f"{x:+.4f} SOL" + (f" (${x * usd:+,.2f})" if usd else "")

    lines = [
        f"# PHANTOM DESK — {mode.upper()} recap",
        f"Window: {_ts(t0)} → {_ts(t1)}",
        "",
        "## Performance",
        f"- Equity: {start_eq:.4f} → {end_eq:.4f} SOL  ({(end_eq / start_eq - 1) * 100 if start_eq else 0:+.2f}%)",
        f"- Window P&L (realized): {sol(s['pnl_sol'])}",
        f"- Cash: {portfolio.cash_sol:.4f} SOL | open positions: {len(portfolio.positions)} "
        f"(value {portfolio.positions_value_sol:.4f} SOL)",
        f"- Since inception: {sol(portfolio.equity_sol - portfolio.starting_cash)} on "
        f"{portfolio.starting_cash:.4f} SOL start ({(portfolio.equity_sol / portfolio.starting_cash - 1) * 100 if portfolio.starting_cash else 0:+.2f}%)",
        f"- Max drawdown: window {max_drawdown(eq_vals):.2f}% | all-time {max_drawdown(eq_all):.2f}%",
        f"- Equity curve (window): {sparkline(eq_vals)}",
        "",
        "## Trades this window",
        f"- {s['n']} closed | {s['wins']}W / {s['losses']}L | win rate {s['win_rate']:.0f}% | "
        f"avg win {s['avg_win_pct']:+.2f}% | avg loss {s['avg_loss_pct']:+.2f}% | profit factor {pf_txt}",
    ]
    for t in trades:
        hold = (t["exit_ts"] - t["entry_ts"]) / 60
        lines += [
            f"### {t['symbol']} {t['pnl_pct']:+.2f}% ({t['pnl_sol']:+.4f} SOL) — held {hold:.0f} min",
            f"- mint `{t['mint']}`",
            f"- WHY ENTERED: {t['entry_reason']}",
            f"- WHY EXITED: {t['exit_reason']} (peak {t['peak_pnl_pct']:+.1f}%)",
        ]
    if portfolio.positions:
        lines += ["", "## Open positions"]
        for p in portfolio.positions.values():
            lines.append(f"- {p.symbol}: P&L {p.pnl_pct:+.2f}% (peak {p.peak_pnl_pct:+.1f}%, "
                         f"trail {'ARMED' if p.trailing_active else 'not armed'}) — {p.entry_reason[:160]}")

    by_stage = Counter((d["stage"], d["outcome"]) for d in decisions)
    filter_rej = [d for d in decisions if d["stage"] == "filter" and d["outcome"] in ("reject", "kill")]
    reasons = Counter()
    for d in filter_rej:
        for part in (d["reason"] or "").split("; "):
            reasons[part.split(":")[0].strip()] += 1
    lines += [
        "", "## Pipeline funnel (logged decisions)",
        f"- Filter: {by_stage[('filter', 'pass')]} passed, {by_stage[('filter', 'reject')]} rejected, "
        f"{by_stage[('filter', 'kill')]} killed permanently",
        f"- Analyst: {by_stage[('analyst', 'approve')]} scored ≥ threshold, {by_stage[('analyst', 'reject')]} below",
        f"- Executor: {by_stage[('executor', 'buy')]} buys, {by_stage[('executor', 'sell')]} sells, "
        f"{by_stage[('executor', 'reject')]} declined at execution",
        "- Top rejection reasons: " + (", ".join(f"{k} ×{v}" for k, v in reasons.most_common(8)) or "none"),
    ]
    analyst_rej = [d for d in decisions if d["stage"] == "analyst"][-12:]
    if analyst_rej:
        lines += ["", "## Latest Analyst verdicts"]
        for d in analyst_rej:
            lines.append(f"- {d['symbol']}: {d['reason'][:220]}")
    exec_rej = [d for d in decisions if d["stage"] == "executor" and d["outcome"] == "reject"][-10:]
    if exec_rej:
        lines += ["", "## Declined at execution"]
        lines += [f"- {d['symbol']}: {d['reason'][:200]}" for d in exec_rej]
    if risk_events:
        lines += ["", "## Risk & incident log"]
        lines += [f"- {_ts(e['ts'])[11:]} [{e['agent']}] {e['symbol'] or ''} {e['message'][:180]}"
                  for e in risk_events[-25:]]
    lines += quant_section(quant)
    lines += shadow_section(store, t0, t1)
    if agents:
        lines += ["", "## Agent health"]
        for name, st in agents.items():
            c = ", ".join(f"{k}={v}" for k, v in sorted(st.counters.items()) if not k.startswith("src:"))
            lines.append(f"- {name}: {st.status}, errors {st.errors} | {c}")
    lines += ["", "## All-time",
              f"- {sa['n']} trades | win rate {sa['win_rate']:.0f}% | realized {sol(sa['pnl_sol'])}"]
    return "\n".join(lines)


def live_gate_status(paper_store: Store, gate) -> tuple[bool, list[str]]:
    trades = paper_store.all_trades()
    eq = [e["equity_sol"] for e in paper_store.equity_series(0)]
    pnl = sum(t["pnl_sol"] for t in trades)
    mdd = max_drawdown(eq)
    msgs, ok = [], True
    if len(trades) < gate.min_paper_trades:
        ok = False
        msgs.append(f"paper trades {len(trades)}/{gate.min_paper_trades}")
    if gate.require_positive_paper_pnl and pnl <= 0:
        ok = False
        msgs.append(f"paper P&L {pnl:+.4f} SOL (must be > 0)")
    if mdd < -gate.max_paper_drawdown_pct:
        ok = False
        msgs.append(f"paper max drawdown {mdd:.1f}% (limit -{gate.max_paper_drawdown_pct}%)")
    if ok:
        msgs.append(f"paper record OK: {len(trades)} trades, P&L {pnl:+.4f} SOL, max DD {mdd:.1f}%")
    return ok, msgs


def next_recap_time(t: float, hours: float) -> float:
    period = hours * 3600
    return (int(t // period) + 1) * period


