"""CONTROLLER (Controllore) — checks that the other agents behave, and estimates better settings.

Two jobs, both read-only. The Controller never changes the running configuration and never trades:
it writes a report of PROPOSALS that the user can accept by editing config.toml.

1. Behaviour checks (every [controller] health_seconds, from memory: no API calls)
   * every agent is alive and making progress (Scout discovers, Filter screens, Analyst scores, Risk polls);
   * the user's hard rules hold on real positions and trades: max open positions, max 8% per trade,
     score >= threshold for every buy, nothing left open under the hard stop, trailing armed after +12%;
   * stops that filled far worse than the stop level, a stalled shadow tracker, queues piling up,
     API rate limits (HTTP 429) being hit, a funnel where nothing passes and the most common reason.

2. Experiments (every [controller] interval_minutes, offline on the desk's own data: no API calls)
   The Logger's shadow tracker follows every evaluated token for 60 minutes and, from this version on,
   stores its price path. On those real tokens the Controller replays different settings:
     Scout     which discovery sources bring tokens worth buying
     Filter    stricter liquidity / volume / top-10 thresholds (never looser than the hard limits)
     Analyst   score threshold and component weights (tokens re-scored offline)
     Risk      hard stop (only tighter), trailing start and distance, time stop, replayed on price paths
     Executor  what the trades it declined (max positions, price impact, round-trip, EV) did afterwards
   Each round tries the current setting, one-change-at-a-time variants and new random combinations, plus
   the best ones from earlier rounds. Results are split by situation (market crash / normal, memecoin
   index up / flat / down, young / mature token, small / large pool).

   Overfitting guard: the best variant is picked on the older 70% of the data and then checked on the
   newest 30% it has never seen. Only a variant that also wins there is proposed, with a 95% interval.
   Every comparison is paired (same tokens, current vs variant), so market luck cancels out.
"""
from __future__ import annotations

import copy
import json
import math
import random
import statistics
import time
from datetime import datetime, timezone

from .market import is_crash

SITUATIONS = {
    "all": "tutte le situazioni",
    "calm": "mercato normale",
    "crash": "mercato in crollo (BTC/SOL/meme in forte calo nell'ora)",
    "meme_up": "memecoin in salita (indice meme 1h > +1.5%)",
    "meme_flat": "memecoin laterali (indice meme 1h tra -1.5% e +1.5%)",
    "meme_down": "memecoin in discesa (indice meme 1h < -1.5%)",
    "young": "token giovani",
    "old": "token maturi",
    "small_pool": "pool piccole (liquidità < $50k)",
    "big_pool": "pool grandi (liquidità >= $50k)",
}
AGENT_NAMES = {"Scout": "Scout", "Filter": "Filter", "Analyst": "Analyst", "Risk": "Risk", "Executor": "Executor"}
BASE_WEIGHTS = {
    "liquidity": 0.14, "turnover": 0.10, "buy_pressure": 0.16, "momentum": 0.12,
    "holders": 0.14, "hygiene": 0.08, "website": 0.16, "maturity": 0.10,
}
SOURCE_KEYS = {"pumpfun": ("scout", "pumpportal"), "dexscreener": ("scout", "dexscreener"),
               "gmgn": ("gmgn", "signal_feed")}


def source_key(src: str) -> tuple[str, str]:
    return SOURCE_KEYS.get(str(src or "?").split(":")[0], ("scout", "geckoterminal"))


# ═════════════════════════════════════════════════════════════════════════════
#  1. Behaviour checks
# ═════════════════════════════════════════════════════════════════════════════
def _f(level: str, agent: str, check: str, detail: str) -> dict:
    return {"level": level, "agent": agent, "check": check, "detail": detail}


def _delta(now: dict, old: dict | None, agent: str, key: str) -> int | None:
    if not old:
        return None
    return int(now["agents"][agent]["counters"].get(key, 0)) - int(old["agents"][agent]["counters"].get(key, 0))


def health_checks(s: dict, old: dict | None = None) -> list[dict]:
    """s: snapshot from Controller.collect(); old: a snapshot ~10 minutes older (None at start). Pure."""
    out: list[dict] = []
    t = s["t"]
    cfg = s["cfg"]
    ag = s["agents"]
    window = (t - old["t"]) / 60 if old else 0.0

    for name, a in ag.items():
        if name == "Controller":
            continue
        if a["status"] == "error":
            out.append(_f("critical", name, "agente in errore", f"ultimo messaggio: {a.get('last', '')[:160]}"))
        if old and name in old["agents"]:
            errs = a["errors"] - old["agents"][name]["errors"]
            if errs >= 20:
                out.append(_f("critical", name, "errori ripetuti", f"{errs} errori negli ultimi {window:.0f} min"))
            elif errs >= 5:
                out.append(_f("warn", name, "errori", f"{errs} errori negli ultimi {window:.0f} min"))

    # ── progress (only judged over a real window) ──
    if old and window >= 5:
        d = lambda a, k: _delta(s, old, a, k) or 0  # noqa: E731
        if d("Scout", "discovered") == 0:
            out.append(_f("warn", "Scout", "nessun token scoperto",
                          f"0 nuovi token in {window:.0f} min: fonti ferme o connessione assente"))
        if d("Scout", "sent") > 0 and d("Filter", "evaluated") == 0:
            out.append(_f("critical", "Filter", "Filter fermo",
                          f"lo Scout ha inviato {d('Scout', 'sent')} token ma il Filter non ne ha valutato nessuno"))
        if s["queues"]["passes"] > 0 and d("Analyst", "scored") == 0:
            out.append(_f("warn", "Analyst", "Analyst fermo",
                          f"{s['queues']['passes']} token in attesa e nessun punteggio in {window:.0f} min"))
        stale, scored = d("Analyst", "stale_pass"), d("Analyst", "scored")
        if stale >= 5 and stale > 0.3 * (stale + scored):
            out.append(_f("warn", "Analyst", "Analyst in ritardo",
                          f"{stale} token scaduti prima del punteggio ({scored} valutati): aumenta [analyst] workers"))
        approved = d("Analyst", "approved")
        handled = d("Executor", "buys") + d("Executor", "declined") + d("Executor", "ev_block")
        if approved >= 3 and handled == 0 and s["queues"]["verdicts"] > 0:
            out.append(_f("warn", "Executor", "Executor non risponde",
                          f"{approved} segnali di acquisto e nessuna decisione dell'Executor in {window:.0f} min"))
        stale_deep = d("Filter", "deep_stale")
        if stale_deep >= 10:
            out.append(_f("info", "Filter", "controlli profondi in ritardo",
                          f"{stale_deep} token scartati perché i dati erano vecchi: valuta [filter] deep_workers"))

    # ── Risk must be watching open positions ──
    pos = s["positions"]
    risk = ag.get("Risk", {})
    if pos and t - risk.get("heartbeat", t) > 30:
        out.append(_f("critical", "Risk", "Risk non sorveglia le posizioni",
                      f"{len(pos)} posizioni aperte e nessun controllo da {t - risk['heartbeat']:.0f} s"))

    # ── hard rules on positions ──
    if len(pos) > cfg["max_open"]:
        out.append(_f("critical", "Executor", "troppe posizioni",
                      f"{len(pos)} aperte, massimo {cfg['max_open']}"))
    for p in pos:
        if p.get("size_pct") is not None and p["size_pct"] > cfg["max_pos_pct"] + 0.05:
            out.append(_f("critical", "Executor", "posizione oltre l'8%",
                          f"{p['symbol']}: {p['size_pct']:.1f}% del saldo all'ingresso (max {cfg['max_pos_pct']}%)"))
        if p["pnl_pct"] <= cfg["hard_stop"] - 2 and not p["exiting"]:
            out.append(_f("critical", "Risk", "posizione sotto lo stop",
                          f"{p['symbol']} a {p['pnl_pct']:+.1f}% (stop {cfg['hard_stop']}%) senza uscita in corso"))
        if p["peak_pct"] >= cfg["trail_activate"] + 1 and not p["trailing"]:
            out.append(_f("warn", "Risk", "trailing non armato",
                          f"{p['symbol']}: picco {p['peak_pct']:+.1f}% ma trailing spento"))
        if p["price_age"] > 60:
            out.append(_f("warn", "Risk", "prezzo vecchio",
                          f"{p['symbol']}: nessun prezzo da {p['price_age']:.0f} s"))

    # ── hard rules on recent trades ──
    for tr in s["trades"]:
        if tr.get("score") is not None and tr["score"] < cfg["min_score"] - 1e-9:
            out.append(_f("critical", "Executor", "acquisto sotto soglia",
                          f"{tr['symbol']} comprato con punteggio {tr['score']:.2f} < {cfg['min_score']}"))
        if (tr.get("exit_reason") or "").startswith("HARD_STOP") and tr["pnl_pct"] < cfg["hard_stop"] - 5:
            out.append(_f("warn", "Executor", "stop eseguito molto peggio",
                          f"{tr['symbol']} chiuso a {tr['pnl_pct']:+.1f}% con stop a {cfg['hard_stop']}% "
                          "(slippage o prezzo lento)"))

    # ── Logger / shadow tracker ──
    if s["queues"]["log"] > 5000:
        out.append(_f("warn", "Logger", "registro in ritardo", f"{s['queues']['log']} eventi da scrivere"))
    sh = s["shadow"]
    if sh["enabled"] and sh["oldest_age"] > sh["horizon"] + 600:
        out.append(_f("warn", "Logger", "follow-up a 60 min bloccato",
                      f"un token è seguito da {sh['oldest_age'] / 60:.0f} min (dovrebbe chiudersi a "
                      f"{sh['horizon'] / 60:.0f})"))
    if s["queues"]["candidates"] >= 4000:
        out.append(_f("warn", "Filter", "coda piena", f"{s['queues']['candidates']} token in coda per il Filter"))

    # ── API limits ──
    if old:
        for host, st in s.get("http", {}).items():
            d429 = st.get("429", 0) - old.get("http", {}).get(host, {}).get("429", 0)
            if d429 >= 20:
                out.append(_f("warn", "Scout" if "gecko" in host or "dexscreener" in host else "Filter",
                              "limite API", f"{host}: {d429} risposte 429 in {window:.0f} min"))

    # ── funnel over the last hour ──
    fu = s["funnel"]
    if fu["filter_seen"] >= 50 and fu["filter_pass"] == 0:
        top = ", ".join(f"{r} ({n})" for r, n in fu["filter_reasons"][:3])
        out.append(_f("warn", "Filter", "nessun token passa i filtri",
                      f"{fu['filter_seen']} valutati nell'ultima ora, 0 promossi. Motivi più frequenti: {top}"))
    if fu["analyst_seen"] >= 10 and fu["analyst_pass"] == 0:
        out.append(_f("info", "Analyst", "nessun punteggio sopra soglia",
                      f"{fu['analyst_seen']} valutati nell'ultima ora, punteggio medio {fu['analyst_avg']:.2f}, "
                      f"massimo {fu['analyst_max']:.2f} (soglia {cfg['min_score']})"))
    if fu["exec_rejects"] >= 5 and fu["exec_buys"] == 0:
        top = ", ".join(f"{r} ({n})" for r, n in fu["exec_reasons"][:3])
        out.append(_f("info", "Executor", "segnali rifiutati dall'Executor",
                      f"{fu['exec_rejects']} rifiuti nell'ultima ora: {top}"))

    if not any(f["level"] in ("warn", "critical") for f in out):
        out.append(_f("ok", "Controller", "tutto regolare", "gli agenti rispettano le regole e lavorano"))
    order = {"critical": 0, "warn": 1, "info": 2, "ok": 3}
    out.sort(key=lambda f: order[f["level"]])
    return out


def reason_category(reason: str) -> str:
    """Short, stable label for a rejection reason (to count the most common ones)."""
    r = (reason or "").lower()
    for key, label in (("volume", "volume 1h"), ("liquidity", "liquidità"), ("top10", "top-10 holder"),
                       ("top-10", "top-10 holder"), ("lp", "LP non bloccata"), ("mint", "mint authority"),
                       ("freeze", "freeze authority"), ("rug", "dev legato a rug"), ("max open", "max posizioni"),
                       ("impact", "impatto sul prezzo"), ("round-trip", "costo andata/ritorno"),
                       ("ev ", "EV negativo"), ("expected value", "EV negativo"), ("revenge", "token già in perdita"),
                       ("pause", "pausa dopo perdite"), ("insider", "insider"), ("sniper", "sniper"),
                       ("bundler", "bundler"), ("stale", "segnale scaduto"), ("expired", "segnale scaduto")):
        if key in r:
            return label
    return r.split(":")[0].split("(")[0].strip()[:40] or "altro"


# ═════════════════════════════════════════════════════════════════════════════
#  2. Experiments
# ═════════════════════════════════════════════════════════════════════════════
def row_situations(f: dict, young_minutes: float) -> list[str]:
    out = ["all"]
    c = is_crash(f)
    if c is not None:
        out.append("crash" if c else "calm")
    m = f.get("mkt_meme_h1")
    if isinstance(m, (int, float)):
        out.append("meme_up" if m > 1.5 else "meme_down" if m < -1.5 else "meme_flat")
    age = f.get("age_min")
    if isinstance(age, (int, float)) and age > 0:
        out.append("young" if age < young_minutes else "old")
    liq = f.get("liq_usd")
    if isinstance(liq, (int, float)):
        out.append("small_pool" if liq < 50_000 else "big_pool")
    return out


def parse_rows(raw: list[dict], young_minutes: float) -> list[dict]:
    rows = []
    for r in raw:
        try:
            f = json.loads(r.get("features") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(f, dict) or r.get("sim_net_pct") is None:
            continue
        path = None
        if r.get("path"):
            try:
                path = [(float(a), float(b)) for a, b in json.loads(r["path"])]
            except (TypeError, ValueError):
                path = None
        if r.get("score") is not None:
            f.setdefault("score", r["score"])
        rows.append({"ts": r["start_ts"], "mint": r.get("mint", ""), "ret": float(r["sim_net_pct"]),
                     "cost": float(r.get("cost_pct") or 2.5), "decision": r.get("decision", ""), "f": f,
                     "path": path, "sit": row_situations(f, young_minutes)})
    rows.sort(key=lambda x: x["ts"])
    return rows


# ── replays ──
def simulate_exit(path: list[tuple[float, float]], cost: float, stop: float, trail_on: float, trail_dist: float,
                  max_hold_min: float = 0) -> float:
    """Net % of the desk exit policy on a recorded price path (raw % moves from the decision price)."""
    peak, trail, last = 0.0, False, -cost
    for sec, raw in path:
        net = raw - cost
        last = net
        if max_hold_min and sec >= max_hold_min * 60:
            return net
        if net <= stop:
            return net
        peak = max(peak, net)
        if peak >= trail_on:
            trail = True
        if trail and 1 + net / 100 <= (1 + peak / 100) * (1 - trail_dist / 100):
            return net
    return last


def analyst_score(f: dict, weights: dict) -> float | None:
    """Re-score a token from its stored components with other base weights (same modifiers as score_token)."""
    comps = {k[2:]: v for k, v in f.items() if k.startswith("c_") and isinstance(v, (int, float))}
    if "raw_score" not in f or not comps:
        return None
    w = {k: weights[k] for k in BASE_WEIGHTS if k in comps}
    if "traction" in comps:
        w["traction"] = weights.get("traction", 0.14)
    if "smart_money" in comps:
        w = {k: v * 0.9 for k, v in w.items()}
        w["smart_money"] = 0.10
    if not f.get("has_site", True) and "website" in w:
        w["website"] *= 0.3
    tot = sum(w.values())
    if tot <= 0:
        return None
    s = sum(w[k] * comps[k] for k in w) / tot
    cap = f.get("cap_score")
    if isinstance(cap, (int, float)):
        s = min(s, cap)
    return max(0.0, min(1.0, s))


def _norm(w: dict) -> dict:
    tot = sum(w.values())
    return {k: round(v / tot, 4) for k, v in w.items()}


class Space:
    """One agent's experiment: the universe of tokens, a way to turn a variant into per-token returns,
    the current variant, and how to generate others."""

    def __init__(self, agent: str, rows: list[dict], current: dict, returns, grid: list[dict],
                 sampler, describe, universe_note: str) -> None:
        self.agent, self.rows, self.current, self.returns = agent, rows, current, returns
        self.grid, self.sampler, self.describe, self.note = grid, sampler, describe, universe_note


def _key(v: dict) -> str:
    return json.dumps(v, sort_keys=True)


def build_spaces(cfg, analyst_rows: list[dict], filter_rows: list[dict], exec_rejects: dict[str, list[str]],
                 min_rows: int) -> list[Space]:
    a, r, f = cfg.analyst, cfg.risk, cfg.filter
    spaces: list[Space] = []
    cur_w = dict(BASE_WEIGHTS)

    # Tokens the current Analyst would buy (when there are enough of them), else every scored token.
    def bought(rows):
        sel = [x for x in rows if isinstance(x["f"].get("score"), (int, float)) and x["f"]["score"] >= a.min_score]
        return (sel, "token con punteggio sopra soglia") if len(sel) >= min_rows else \
            (rows, "tutti i token valutati dall'Analyst (quelli sopra soglia sono ancora pochi)")

    # ── Analyst ──
    rescorable = [x for x in analyst_rows if analyst_score(x["f"], cur_w) is not None]
    if len(rescorable) >= min_rows:
        rows, note = rescorable, "token valutati dall'Analyst, ri-calcolati con pesi diversi"

        def a_ret(v, rows=rows):
            return [x["ret"] if (analyst_score(x["f"], v["weights"]) or 0) >= v["min_score"] else 0.0 for x in rows]
        grid = [{"min_score": ms, "weights": cur_w} for ms in (0.6, 0.65, 0.7, 0.75, 0.8) if ms >= a.min_score]
        for k in BASE_WEIGHTS:
            for mult in (0.5, 1.5):
                grid.append({"min_score": a.min_score, "weights": _norm({**cur_w, k: cur_w[k] * mult})})

        def a_sample(rng):
            return {"min_score": rng.choice([m for m in (0.6, 0.62, 0.65, 0.68, 0.7, 0.75, 0.8) if m >= a.min_score]
                                            or [a.min_score]),
                    "weights": _norm({k: w * rng.choice((0.5, 0.75, 1.0, 1.25, 1.5, 2.0)) for k, w in cur_w.items()})}
    else:
        rows, note = analyst_rows, "token valutati dall'Analyst (pesi non ancora testabili: dati nuovi in arrivo)"

        def a_ret(v, rows=rows):
            return [x["ret"] if (x["f"].get("score") or 0) >= v["min_score"] else 0.0 for x in rows]
        grid = [{"min_score": ms, "weights": cur_w} for ms in (0.6, 0.65, 0.7, 0.75, 0.8) if ms >= a.min_score]

        def a_sample(rng):
            return {"min_score": rng.choice([m for m in (0.6, 0.62, 0.65, 0.68, 0.7, 0.75, 0.8) if m >= a.min_score]
                                            or [a.min_score]), "weights": cur_w}

    def a_desc(v):
        parts = []
        if abs(v["min_score"] - a.min_score) > 1e-9:
            parts.append(f"[analyst] min_score = {v['min_score']:g}")
        ch = [f"{k} {cur_w[k]:.2f}->{v['weights'][k]:.2f}" for k in cur_w if abs(v["weights"][k] - cur_w[k]) > 0.005]
        if ch:
            parts.append("pesi del punteggio (desk/agents/analyst.py WEIGHTS): " + ", ".join(ch))
        return parts
    spaces.append(Space("Analyst", rows, {"min_score": a.min_score, "weights": cur_w}, a_ret, grid, a_sample,
                        a_desc, note))

    # ── Risk (exit rules replayed on price paths) ──
    with_path = [x for x in analyst_rows if x["path"]]
    rows, note = bought(with_path)
    stops = [s for s in (-6.0, -5.0, -4.0, -3.0) if s >= r.hard_stop_pct]
    trail_on = sorted({max(12.0, r.trail_activate_pct), 12.0, 15.0, 20.0, 25.0, 30.0, 40.0})
    trail_on = [x for x in trail_on if x >= 12.0]
    dists = [3.0, 4.0, 5.0, 6.0, 8.0, 10.0]
    holds = [0.0, 15.0, 30.0, 45.0]
    cur_r = {"hard_stop_pct": r.hard_stop_pct, "trail_activate_pct": r.trail_activate_pct,
             "trail_distance_pct": r.trail_distance_pct, "max_hold_minutes": float(r.max_hold_minutes)}

    def r_ret(v, rows=rows):
        return [simulate_exit(x["path"], x["cost"], v["hard_stop_pct"], v["trail_activate_pct"],
                              v["trail_distance_pct"], v["max_hold_minutes"]) for x in rows]
    grid = [{**cur_r, "hard_stop_pct": s} for s in stops] + [{**cur_r, "trail_activate_pct": x} for x in trail_on] \
        + [{**cur_r, "trail_distance_pct": x} for x in dists] + [{**cur_r, "max_hold_minutes": x} for x in holds]

    def r_sample(rng):
        return {"hard_stop_pct": rng.choice(stops), "trail_activate_pct": rng.choice(trail_on),
                "trail_distance_pct": rng.choice(dists), "max_hold_minutes": rng.choice(holds)}

    def r_desc(v):
        return [f"[risk] {k} = {v[k]:g}" for k in cur_r if abs(v[k] - cur_r[k]) > 1e-9]
    spaces.append(Space("Risk", rows, cur_r, r_ret, grid, r_sample, r_desc, note + ", con percorso di prezzo"))

    # ── Filter (only stricter than now: looser would let unchecked tokens through) ──
    passed = [x for x in filter_rows if x["decision"] in ("approve", "pass")]
    young_min = a.young_age_hours * 60
    cur_f = {"min_liquidity_usd": f.min_liquidity_usd, "max_top10_pct": f.max_top10_pct,
             "min_volume_h1_usd": f.min_volume_h1_usd}
    liqs = sorted({f.min_liquidity_usd * m for m in (1, 1.5, 2, 3, 5)})
    tops = sorted({x for x in (f.max_top10_pct, 18.0, 15.0, 12.0, 10.0) if x <= f.max_top10_pct})
    vols = sorted({x for x in (f.min_volume_h1_usd, 75_000.0, 100_000.0, 150_000.0, 250_000.0)
                   if x >= f.min_volume_h1_usd})

    def f_keep(x, v):
        g = x["f"]
        if (g.get("liq_usd") or 0) < v["min_liquidity_usd"]:
            return False
        t10 = g.get("top10_pct")
        if isinstance(t10, (int, float)) and t10 > v["max_top10_pct"]:
            return False
        age = g.get("age_min") or 0
        return not (age >= young_min and (g.get("vol_h1") or 0) < v["min_volume_h1_usd"])

    def f_ret(v, rows=passed):
        return [x["ret"] if f_keep(x, v) else 0.0 for x in rows]
    grid = [{**cur_f, "min_liquidity_usd": x} for x in liqs] + [{**cur_f, "max_top10_pct": x} for x in tops] \
        + [{**cur_f, "min_volume_h1_usd": x} for x in vols]

    def f_sample(rng):
        return {"min_liquidity_usd": rng.choice(liqs), "max_top10_pct": rng.choice(tops),
                "min_volume_h1_usd": rng.choice(vols)}

    def f_desc(v):
        return [f"[filter] {k} = {v[k]:g}" for k in cur_f if abs(v[k] - cur_f[k]) > 1e-9]
    spaces.append(Space("Filter", passed, cur_f, f_ret, grid, f_sample, f_desc,
                        "token promossi dal Filter (solo soglie più severe, mai più larghe)"))

    # ── Scout (discovery sources) ──
    rows, note = bought(analyst_rows)
    srcs = sorted({"{}.{}".format(*source_key(x["f"].get("source"))) for x in rows})
    cur_s = {s: True for s in srcs}

    def s_ret(v, rows=rows):
        return [x["ret"] if v.get("{}.{}".format(*source_key(x["f"].get("source"))), True) else 0.0 for x in rows]
    grid = [{**cur_s, s: False} for s in srcs] if len(srcs) > 1 else []

    def s_sample(rng):
        v = {s: rng.random() > 0.3 for s in srcs}
        if not any(v.values()) and srcs:
            v[rng.choice(srcs)] = True
        return v

    def s_desc(v):
        return [f"[{s.split('.')[0]}] {s.split('.')[1]} = false" for s in srcs if not v.get(s, True)]
    spaces.append(Space("Scout", rows, cur_s, s_ret, grid, s_sample if len(srcs) > 1 else (lambda rng: cur_s),
                        s_desc, note))

    # ── Executor (tokens it declined, by reason: what would buying them have done?) ──
    by_mint = {x["mint"]: x for x in analyst_rows}
    cats = {}
    for mint, reasons in exec_rejects.items():
        x = by_mint.get(mint)
        if x is None:
            continue
        for rc in set(reasons):
            cats.setdefault(rc, []).append(x)
    rows = [x for xs in cats.values() for x in xs]
    rows = list({id(x): x for x in rows}.values())
    cat_names = sorted(cats)
    cur_e = {c: False for c in cat_names}       # False = keep declining (current behaviour)

    def e_ret(v, rows=rows, cats=cats):
        allow = {id(x) for c, xs in cats.items() if v.get(c) for x in xs}
        return [x["ret"] if id(x) in allow else 0.0 for x in rows]
    grid = [{**cur_e, c: True} for c in cat_names]

    def e_desc(v):
        hint = {"max posizioni": "[executor] max_open_positions più alto",
                "impatto sul prezzo": "[executor] max_entry_price_impact_pct più alto",
                "costo andata/ritorno": "[executor] max_round_trip_loss_pct più alto (attenzione: protegge "
                                        "dagli honeypot)",
                "EV negativo": "[quant] gate EV meno severo"}
        return [f"accettare i token rifiutati per '{c}'" + (f" ({hint[c]})" if c in hint else "")
                for c in cat_names if v.get(c)]
    spaces.append(Space("Executor", rows, cur_e, e_ret, grid, lambda rng: cur_e, e_desc,
                        "segnali di acquisto rifiutati dall'Executor, seguiti per 60 minuti"))
    return spaces


def paired(diff: list[float], z: float = 1.96) -> dict:
    """Mean of paired differences with a normal interval (z=1.96: 95%; 2.58: 99%, used for the many
    sub-situations so that luck in one of ten slices is less likely to look like an edge)."""
    n = len(diff)
    if n == 0:
        return {"n": 0, "mean": 0.0, "lo": 0.0, "hi": 0.0}
    m = statistics.fmean(diff)
    sd = statistics.stdev(diff) if n > 1 else 0.0
    h = z * sd / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": m, "lo": m - h, "hi": m + h}


def evaluate_space(sp: Space, rng: random.Random, n_random: int, previous: list[dict], holdout: float,
                   min_rows: int) -> tuple[list[dict], int]:
    """Try current + grid + random + previous winners on every situation. Returns (results, variants tried)."""
    if len(sp.rows) < min_rows:
        return [], 0
    variants = {_key(sp.current): sp.current}
    for v in sp.grid + [sp.sampler(rng) for _ in range(n_random)] + previous:
        variants.setdefault(_key(v), v)
    base = sp.returns(sp.current)
    vecs = {k: sp.returns(v) for k, v in variants.items()}
    results = []
    for sit in SITUATIONS:
        idx = [i for i, x in enumerate(sp.rows) if sit in x["sit"]]
        if len(idx) < min_rows:
            continue
        cut = int(len(idx) * (1 - holdout))
        train, valid = idx[:cut], idx[cut:]
        best = None
        for k, vec in vecs.items():
            if k == _key(sp.current):
                continue
            d_train = [vec[i] - base[i] for i in train]
            changed = sum(1 for i in train if abs(vec[i] - base[i]) > 1e-9)
            if changed < 3:
                continue
            st = paired(d_train)
            if best is None or st["mean"] > best[1]["mean"]:
                best = (k, st, changed)
        cur_sel = [base[i] for i in idx]
        entry = {"agent": sp.agent, "situation": sit, "n": len(idx), "note": sp.note,
                 "current_avg": statistics.fmean(cur_sel), "current_taken": sum(1 for v in cur_sel if v != 0)}
        if best is None:
            results.append({**entry, "verdict": "nessuna variante cambia abbastanza token"})
            continue
        k, st, changed = best
        vec = vecs[k]
        va = paired([vec[i] - base[i] for i in valid], z=1.96 if sit == "all" else 2.58)
        if st["mean"] > 0 and va["n"] >= max(10, min_rows // 3) and va["lo"] > 0:
            verdict = "solida"
        elif st["mean"] > 0 and va["mean"] > 0:
            verdict = "promettente"
        else:
            verdict = "nessun miglioramento"
        results.append({**entry, "variant": variants[k], "changes": sp.describe(variants[k]), "train": st,
                        "valid": va, "changed": changed, "verdict": verdict,
                        "variant_taken": sum(1 for i in idx if vec[i] != 0)})
    return results, len(variants)


def top_variants(results: list[dict], limit: int = 5) -> list[dict]:
    seen, out = set(), []
    for r in sorted((r for r in results if "variant" in r), key=lambda r: -r["train"]["mean"]):
        k = _key(r["variant"])
        if k not in seen:
            seen.add(k)
            out.append(r["variant"])
        if len(out) >= limit:
            break
    return out


def run_experiments(cfg, analyst_raw: list[dict], filter_raw: list[dict], exec_rejects: dict[str, list[str]],
                    previous: dict[str, list[dict]], seed: int | None = None) -> dict:
    """One round of tests. Pure CPU, no I/O: safe to run in a worker thread."""
    c = cfg.controller
    young = cfg.analyst.young_age_hours * 60
    ar = parse_rows(analyst_raw, young)
    fr = parse_rows(filter_raw, young)
    rng = random.Random(seed if seed is not None else time.time_ns())
    out = {"t": time.time(), "analyst_rows": len(ar), "with_path": sum(1 for x in ar if x["path"]),
           "filter_rows": len(fr), "since": min([x["ts"] for x in ar + fr], default=None),
           "results": [], "tried": 0, "best": {}}
    for sp in build_spaces(cfg, ar, fr, exec_rejects, c.min_rows):
        res, tried = evaluate_space(sp, rng, c.random_variants, previous.get(sp.agent, []), c.holdout_pct / 100,
                                    c.min_rows)
        out["results"] += res
        out["tried"] += tried
        out["best"][sp.agent] = top_variants(res)
        if not res:
            out["results"].append({"agent": sp.agent, "situation": "all", "n": len(sp.rows), "note": sp.note,
                                   "verdict": f"dati insufficienti ({len(sp.rows)} token, ne servono {c.min_rows})"})
    return out


def within_hard_limits(cfg, changes: list[str]) -> bool:
    """True if every '[section] key = value' change survives enforce_hard_limits unchanged."""
    from .config import enforce_hard_limits
    test = copy.deepcopy(cfg)
    test.warnings = []
    for ch in changes:
        if not ch.startswith("[") or " = " not in ch:
            continue
        sec, rest = ch[1:].split("] ", 1)
        key, val = rest.split(" = ", 1)
        obj = getattr(test, sec, None)
        if obj is None or not hasattr(obj, key):
            return False
        cur = getattr(obj, key)
        v = (val == "true") if isinstance(cur, bool) else type(cur)(float(val)) if isinstance(cur, (int, float)) \
            else val
        setattr(obj, key, v)
    enforce_hard_limits(test)
    return not any(w.startswith("HARD LIMIT") for w in test.warnings)


# ═════════════════════════════════════════════════════════════════════════════
#  3. Report
# ═════════════════════════════════════════════════════════════════════════════
LEVEL = {"critical": "CRITICO", "warn": "ATTENZIONE", "info": "info", "ok": "ok"}


def proposals(exp: dict, cfg) -> list[dict]:
    """Winning variants within the hard limits. The same change winning in several situations is one
    proposal (shown with the best-measured situation and the list of the others)."""
    cands = []
    for r in exp.get("results", []):
        if r.get("verdict") not in ("solida", "promettente") or not r.get("changes"):
            continue
        if not within_hard_limits(cfg, r["changes"]):
            continue
        cands.append({"agent": r["agent"], "situation": r["situation"], "label": SITUATIONS[r["situation"]],
                      "changes": r["changes"], "verdict": r["verdict"], "gain": r["valid"]["mean"],
                      "lo": r["valid"]["lo"], "hi": r["valid"]["hi"], "n": r["n"], "valid_n": r["valid"]["n"]})
    cands.sort(key=lambda p: (p["verdict"] != "solida", p["situation"] != "all", -p["gain"]))
    groups: dict[tuple, dict] = {}
    for p in cands:
        k = (p["agent"], tuple(p["changes"]))
        if k in groups:
            groups[k]["also"].append(p["label"])
        else:
            groups[k] = {**p, "also": []}
    return list(groups.values())


def build_report(health: list[dict], exp: dict | None, cfg, trials_total: int, rounds: int) -> str:
    t = time.time()
    lines = [f"# Controllore — report del {datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M} UTC", "",
             "Il Controllore verifica gli agenti e prova configurazioni diverse sui dati reali registrati dal "
             "Logger. Non cambia nulla da solo e non fa trade: qui sotto ci sono solo PROPOSTE.", "",
             "## Comportamento degli agenti"]
    for f in health:
        lines.append(f"- [{LEVEL[f['level']]}] {f['agent']} — {f['check']}: {f['detail']}")
    lines.append("")
    if not exp:
        lines.append("Primo ciclo di test non ancora eseguito.")
        return "\n".join(lines)
    since = exp.get("since")
    lines += ["## Dati usati",
              f"- {exp['analyst_rows']} token valutati dall'Analyst ({exp['with_path']} con percorso di prezzo), "
              f"{exp['filter_rows']} token valutati dal Filter, ciascuno seguito per 60 minuti"
              + (f", dal {datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d %H:%M} UTC" if since else ""),
              f"- {exp['tried']} configurazioni provate in questo ciclo, {trials_total} in totale in {rounds} cicli",
              f"- scelta sul {100 - cfg.controller.holdout_pct:.0f}% più vecchio dei dati, verifica sul "
              f"{cfg.controller.holdout_pct:.0f}% più recente", ""]
    props = proposals(exp, cfg)
    lines.append("## Proposte")
    if not props:
        lines.append("Nessuna proposta per ora: nessuna variante batte la configurazione attuale anche sui dati di "
                     "verifica. Più dati arrivano, più i test diventano precisi.")
    for p in props:
        lines.append(f"- {p['agent']} | {p['label']} | {'; '.join(p['changes'])}")
        if p["also"]:
            lines.append(f"    vince anche in: {', '.join(p['also'])}")
        lines.append(f"    guadagno stimato {p['gain']:+.2f} punti % per token valutato, intervallo 95% "
                     f"[{p['lo']:+.2f}, {p['hi']:+.2f}] su {p['valid_n']} token di verifica — {p['verdict'].upper()}")
    lines.append("")
    lines.append("## Dettaglio per agente e situazione")
    for agent in ("Scout", "Filter", "Analyst", "Executor", "Risk"):
        rs = [r for r in exp["results"] if r["agent"] == agent]
        if not rs:
            continue
        lines.append(f"### {agent} — {rs[0].get('note', '')}")
        for r in rs:
            head = f"- {SITUATIONS.get(r['situation'], r['situation'])} ({r['n']} token)"
            if "variant" not in r:
                lines.append(f"{head}: {r['verdict']}")
                continue
            ch = "; ".join(r["changes"]) or "nessuna modifica"
            lines.append(f"{head}: attuale media {r['current_avg']:+.2f}% per token ({r['current_taken']} presi) | "
                         f"migliore: {ch} -> {r['train']['mean']:+.2f} (scelta), {r['valid']['mean']:+.2f} "
                         f"[{r['valid']['lo']:+.2f}, {r['valid']['hi']:+.2f}] (verifica), "
                         f"{r['variant_taken']} presi — {r['verdict']}")
        lines.append("")
    lines += ["## Come leggere", "- 'per token valutato': differenza media di risultato netto (dopo i costi) "
              "rispetto alla configurazione attuale, contando 0 per i token non comprati.",
              "- SOLIDA: migliore anche sui dati di verifica, con intervallo al 95% tutto sopra zero. PROMETTENTE: "
              "migliore in media ma ancora incerta. Le proposte rispettano sempre i limiti rigidi (8%, -6%, "
              "+12%, soglia 0.6, top-10 20%, volume $50k).",
              "- Una proposta per una sola situazione (es. mercato in crollo) oggi si applica cambiando il valore "
              "generale: valuta se la situazione è frequente. Prova ogni modifica prima in paper.",
              "- Il percorso di prezzo è campionato ogni ~20 s: gli stop reali (ogni 2 s) possono uscire a prezzi "
              "leggermente diversi. Sono stime sul passato, non garanzie."]
    return "\n".join(lines)
