"""EXIT STUDY — where should the profit lock arm and sell? Measured on the logged price paths (read only).

User's request (2026-11-11): the profit stop must not close a position too early because of the wide swings of
the first hours after a launch. Arm it only once the profit has covered the fees (100% of the round-trip cost)
or, with fees under 3%, once the profit is around +7%; then the stop sits around +4%. The exact percentages are
to be measured, not taken literally. This module replays many lock settings on every Analyst follow-up price
path (60 min, same replay as the Controller: entry timing, stop, trailing, time exit) and on the real trades:

  * mean / median net result per token, losing trades, worst 10%, on the older and the newer half separately
  * "uscite premature": lock sales after which the token still went 10+ points higher within the hour
  * young tokens (age < [analyst] young_age_hours) apart, since the user worries about the first hours
  * every real PROFIT_LOCK / TRAILING_STOP trade: what the price did after we sold

python run.py exit-study prints it and saves data/reports/exit_study.md. It never trades or changes settings.
"""
from __future__ import annotations

import json
import re
import time
from itertools import product
from statistics import mean, median

from .controller import clip_ret, parse_rows
from .exits import LockRule, lock_rule, lock_step, lock_trigger

EARLY_PTS = 10.0     # a lock sale is "premature" if the token later went this many points above the sale


def replay(path, cost: float, stop: float, trail_on: float, trail_dist: float, max_hold_min: float,
           lock: LockRule) -> tuple[float, str, float]:
    """Same exit order as controller.simulate_exit. Returns (net %, exit kind, max net % reached after the exit)."""
    peak, trail, last = 0.0, False, -cost
    touch, armed = None, 0.0
    kind, out, at = "END", None, len(path)
    for i, (sec, raw) in enumerate(path):
        net = raw - cost
        last = net
        if max_hold_min and sec >= max_hold_min * 60:
            kind, out, at = "TIME", net, i
            break
        if net <= stop:
            kind, out, at = "STOP", net, i
            break
        peak = max(peak, net)
        if peak >= trail_on:
            trail = True
        if trail and 1 + net / 100 <= (1 + peak / 100) * (1 - trail_dist / 100):
            kind, out, at = "TRAIL", net, i
            break
        if lock.enabled:
            touch, armed = lock_step(lock, peak, sec, touch, armed, cost)
            if armed and net <= lock_trigger(lock, peak):
                kind, out, at = "LOCK", net, i
                break
    if out is None:
        out = last
    after = max((raw - cost for _, raw in path[at + 1:]), default=out)
    return clip_ret(out), kind, clip_ret(after)


def _rule(on: float, at: float, keep: float, fee: float, fast_on: float, fast_min: float, buffer: float) -> LockRule:
    return lock_rule({"profit_lock_activate_pct": on, "profit_lock_fast_activate_pct": fast_on,
                      "profit_lock_fast_minutes": fast_min, "profit_lock_pct": at, "profit_lock_keep_pct": keep,
                      "profit_lock_fee_multiple": fee}, buffer)


def evaluate(rows: list[dict], r, rule_for) -> dict:
    """rule_for(row) -> LockRule. Returns the metrics of one variant on rows."""
    stop = r.exit_stop_pct or r.hard_stop_pct
    res, early, locks, missed = [], 0, 0, []
    for x in rows:
        net, kind, after = replay(x["epath"], x["cost"], stop, r.trail_activate_pct, r.trail_distance_pct,
                                  float(r.max_hold_minutes), rule_for(x))
        res.append(net)
        if kind == "LOCK":
            locks += 1
            missed.append(after - net)
            if after - net >= EARLY_PTS:
                early += 1
    n = len(res)
    if not n:
        return {"n": 0}
    srt = sorted(res)
    k = max(1, n // 10)
    h = n // 2
    return {"n": n, "mean": mean(res), "median": median(res), "losers": sum(1 for v in res if v < 0),
            "worst10": mean(srt[:k]), "old": mean(res[:h]) if h else 0.0, "new": mean(res[h:]),
            "locks": locks, "early": early, "missed": mean(missed) if missed else 0.0, "res": res}


def _fmt(m: dict) -> str:
    if not m.get("n"):
        return "nessun dato"
    return (f"media {m['mean']:+.2f}% | mediana {m['median']:+.2f}% | in perdita {m['losers']}/{m['n']} | "
            f"peggior 10% {m['worst10']:+.1f}% | metà vecchia {m['old']:+.2f} / nuova {m['new']:+.2f} | "
            f"uscite a profitto {m['locks']}, premature {m['early']} (dopo la vendita in media "
            f"+{m['missed']:.1f} punti più su)")


def _paired(a: list[float], b: list[float]) -> tuple[float, float, float]:
    d = [x - y for x, y in zip(a, b)]
    if len(d) < 2:
        return (d[0] if d else 0.0), 0.0, 0.0
    mu = mean(d)
    sd = (sum((v - mu) ** 2 for v in d) / (len(d) - 1)) ** 0.5
    se = sd / len(d) ** 0.5
    return mu, mu - 1.96 * se, mu + 1.96 * se


def variants(r) -> list[tuple[str, dict]]:
    out = []
    for on, at, keep, fee, fast in product((5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0, 15.0),
                                           (2.0, 3.0, 4.0, 5.0, 6.0, 8.0), (0.0, 30.0, 50.0),
                                           (0.0, 1.0, 2.0), (0.0, 2.0)):
        if at > on - 2:
            continue
        out.append((f"arma +{on:g}%{' (+' + format(on + fast, 'g') + '% se rapido)' if fast else ''}"
                    f"{', e almeno ' + format(fee, 'g') + 'x commissioni' if fee else ''}, "
                    f"vende a +{at:g}%{', o ' + format(keep, 'g') + '% del picco' if keep else ' fisso'}",
                    {"on": on, "at": at, "keep": keep, "fee": fee, "fast_on": on + fast}))
    return out


def real_trades(store, analyst_raw: list[dict]) -> list[str]:
    """What the price did after each real lock / trailing sale (from the 60-min follow-up of the same token)."""
    paths: dict[str, list[tuple[float, list]]] = {}
    for x in analyst_raw:
        if x.get("path"):
            try:
                paths.setdefault(x["mint"], []).append((float(x["start_ts"]), json.loads(x["path"])))
            except (TypeError, ValueError):
                pass
    trades = store.q("SELECT mint,symbol,entry_ts,exit_ts,pnl_pct,peak_pnl_pct,exit_reason,entry_reason FROM trades "
                     "UNION ALL SELECT mint,symbol,entry_ts,exit_ts,pnl_pct,peak_pnl_pct,exit_reason,entry_reason "
                     "FROM trades_archive ORDER BY entry_ts")
    out = []
    for t in trades:
        kind = (t["exit_reason"] or "").split(":")[0]
        if kind not in ("PROFIT_LOCK", "TRAILING_STOP"):
            continue
        m = re.search(r"round-trip ([\d.]+)%", t["entry_reason"] or "")
        rt = float(m.group(1)) if m else 0.0
        line = (f"- {t['symbol']} {time.strftime('%m-%d %H:%M', time.gmtime(t['entry_ts']))} UTC: {kind}, "
                f"picco {t['peak_pnl_pct']:+.1f}%, venduto a {t['pnl_pct']:+.2f}%")
        best = None
        for start, p in paths.get(t["mint"], []):
            if start - 600 <= t["entry_ts"] <= start + 3600:
                best = (start, p)
        if not best:
            out.append(line + " — nessun percorso di prezzo registrato per questo token")
            continue
        start, p = best
        e_off, x_off = t["entry_ts"] - start, t["exit_ts"] - start
        e_raw = min(p, key=lambda s: abs(s[0] - e_off))[1]
        after = [(s, (1 + v / 100) / (1 + e_raw / 100) * 100 - 100 - rt) for s, v in p if s > x_off]
        if not after:
            out.append(line + " — il percorso finisce prima della vendita")
            continue
        top = max(after, key=lambda s: s[1])
        out.append(line + f"; dopo la vendita (stesso conto netto): massimo {top[1]:+.1f}% dopo "
                          f"{(top[0] - x_off) / 60:.0f} min, a fine ora {after[-1][1]:+.1f}%")
    return out or ["- nessun trade chiuso da stop a profitto o trailing"]


def run_study(cfg, store) -> str:
    since = time.time() - cfg.controller.lookback_days * 86400
    cols = "start_ts,mint,score,decision,sim_net_pct,cost_pct,features,path"
    raw = store.q(f"SELECT {cols} FROM shadow WHERE stage='analyst' AND features IS NOT NULL AND start_ts>=? "
                  "ORDER BY start_ts", (since,))
    young_h = float(cfg.analyst.young_age_hours)
    skipped: dict = {}
    rows = [x for x in parse_rows(raw, young_h * 60, cfg, skipped) if x.get("epath")]
    approved = [x for x in rows if (x["f"].get("score") or 0) >= cfg.analyst.min_score]
    r = cfg.risk
    buf = float(r.profit_lock_sell_buffer_pct)
    cur = lock_rule(r, buf)
    sets = [("token approvati (punteggio sopra soglia)", approved), ("tutti i token valutati dall'Analyst", rows)]
    costs = sorted(x["cost"] for x in rows)
    ages = [x["f"].get("age_min") for x in rows if isinstance(x["f"].get("age_min"), (int, float))]
    lines = [f"# Studio dello stop a profitto — {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC", "",
             f"Percorsi di prezzo: {len(rows)} token (di cui {len(approved)} sopra soglia), esclusi {skipped}. "
             f"Regole d'uscita attuali per il resto: stop {r.exit_stop_pct or r.hard_stop_pct}%, trailing da "
             f"+{r.trail_activate_pct:g}% a {r.trail_distance_pct:g}%, uscita a tempo {r.max_hold_minutes:g} min, "
             f"costi di vendita +{buf:g}%.",
             f"Commissioni andata/ritorno: mediana {costs[len(costs) // 2]:.2f}%, sotto il 3% "
             f"{sum(1 for c in costs if c < 3)}/{len(costs)}, massimo {costs[-1]:.2f}%." if costs else "",
             f"Età dei token all'acquisto: mediana {median(ages) / 60:.1f} h, sotto {young_h:g} h "
             f"{sum(1 for a in ages if a < young_h * 60)}/{len(ages)}." if ages else "", ""]
    named = [("stop a profitto spento", lambda x: LockRule()), ("attuale", lambda x: cur),
             ("notte (arma +5%/+6% rapido, vende a +2.5% o 50% del picco)",
              lambda x: _rule(5, 2.5, 50, 0, 6, 3, buf))]
    grid = variants(r)
    for title, data in sets:
        if len(data) < 5:
            lines += [f"## {title}", "pochi dati", ""]
            continue
        base = evaluate(data, r, lambda x: cur)
        lines += [f"## {title} ({len(data)} token)"]
        for name, fn in named:
            lines.append(f"- {name}: {_fmt(evaluate(data, r, fn))}")
        scored = []
        for name, v in grid:
            rule = _rule(v["on"], v["at"], v["keep"], v["fee"], v["fast_on"], float(r.profit_lock_fast_minutes), buf)
            m = evaluate(data, r, lambda x, rule=rule: rule)
            mu, lo, hi = _paired(m["res"], base["res"])
            scored.append((m["mean"], name, m, mu, lo, hi))
        safe = [s for s in scored if s[2]["losers"] <= base["losers"] and s[2]["worst10"] >= base["worst10"] - 1]
        safe.sort(key=lambda s: -s[0])
        lines += ["", "Migliori varianti che non aggiungono trade in perdita né peggiorano il 10% peggiore "
                      "(differenza rispetto all'attuale, intervallo 95%):"]
        for _, name, m, mu, lo, hi in safe[:15]:
            lines.append(f"- {name}: {mu:+.2f} [{lo:+.2f}, {hi:+.2f}] | {_fmt(m)}")
        # Young vs old: the same best few, split by token age
        young = [x for x in data if isinstance(x["f"].get("age_min"), (int, float)) and x["f"]["age_min"] < young_h * 60]
        yid = {id(x) for x in young}
        old = [x for x in data if id(x) not in yid]
        lines += ["", f"Per età del token (giovani < {young_h:g} h: {len(young)}, più vecchi: {len(old)}):"]
        for name, fn in named[1:2] + [(s[1], None) for s in safe[:3]]:
            if fn is None:
                v = dict(grid)[name]
                rule = _rule(v["on"], v["at"], v["keep"], v["fee"], v["fast_on"], float(r.profit_lock_fast_minutes), buf)
                fn = (lambda rr: (lambda x: rr))(rule)
            lines.append(f"- {name}: giovani {_fmt(evaluate(young, r, fn)) if young else '-'} || "
                         f"vecchi {_fmt(evaluate(old, r, fn)) if old else '-'}")
        # Young tokens arm later: extra points for the first hours (user's worry), on top of the best safe rule
        if safe and young:
            v = dict(grid)[safe[0][1]]
            lines += ["", f"Soglia più alta solo per i token giovani (sopra '{safe[0][1]}'):"]
            for extra in (0.0, 2.0, 3.0, 5.0, 8.0):
                ry = _rule(v["on"] + extra, v["at"], v["keep"], v["fee"], v["fast_on"] + extra,
                           float(r.profit_lock_fast_minutes), buf)
                ro = _rule(v["on"], v["at"], v["keep"], v["fee"], v["fast_on"], float(r.profit_lock_fast_minutes), buf)
                m = evaluate(data, r, lambda x, ry=ry, ro=ro: ry if id(x) in yid else ro)
                lines.append(f"- giovani +{extra:g} punti: {_fmt(m)}")
        lines.append("")
    lines += ["## Trade reali chiusi in profitto: cosa ha fatto il prezzo dopo la vendita"]
    lines += real_trades(store, raw)
    lines += ["", "Stime sul passato (percorsi campionati ogni ~20 s), non garanzie."]
    return "\n".join(x for x in lines if x is not None)
