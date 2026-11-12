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
               + stops wider than -6% (to -30%) with wider trailing / time exits: MEASURE ONLY, shown in their
                 own report section, never proposed (the -6% stop is a hard limit, changing it is the user's call)
     Timing    when to buy an approved token (at once / dip / bounce / confirmation, desk/entry.py), on the paths
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

from .entry import MAX_WINDOW_MINUTES, entry_path, settings as entry_settings
from .exits import OFF, LockRule, lock_level, lock_rule, lock_step, rise_speed
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
WIDE_STOPS = "Risk (stop larghi)"     # measure-only experiment: stops wider than the -6% hard limit
AGENT_NAMES = {"Scout": "Scout", "Filter": "Filter", "Analyst": "Analyst", "Risk": "Risk", "Executor": "Executor",
               "Timing": "Timing (momento d'acquisto)"}
# One token's result is capped to this range in every test: a single price glitch (or one +50,000% runner)
# must not decide the average for 80 others. A real exit beyond +300% in an hour is rare and still counts as +300%.
RET_MIN, RET_MAX = -100.0, 300.0


SKIP_TEXT = {"copycat": "copycat (mint non creato da pump.fun, ora bloccati dal Filter)",
             "frozen": "con prezzo congelato (pool svuotata: DexScreener smette di aggiornarla)",
             "glitch": "con una lettura di prezzo assurda"}


# The Controller never arms the profit lock under this net % (user's rule 2026-11-11: avoid early exits caused
# by the wide swings right after a launch; their own figure was about +7%, measured choice below that: +6%).
LOCK_MIN_ON = 6.0


def clip_ret(x: float) -> float:
    return max(RET_MIN, min(RET_MAX, x))


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


def path_problem(path: list[tuple[float, float]] | None) -> str:
    """Why a recorded price path can't be trusted ("" = fine).

    frozen: DexScreener stops updating a pool once its liquidity is pulled, so the follow-up reads one flat price
      for the whole hour. Measured 2026-11-11: 24 of the 27 'pumpswap:pool' follow-ups were flat, 22 of those 24
      pools had $0 liquidity the next day (rugs recorded as +2%, +475%...). Pump.fun migrations: 0 flat of 81.
    glitch: one absurd reading (e.g. +1,376,622% for a token that ended the hour at +23%)."""
    if not path:
        return ""
    if len(path) >= 15 and len({v for _, v in path}) <= 2:
        return "frozen"
    if max(v for _, v in path) > 5000:
        return "glitch"
    return ""


def parse_rows(raw: list[dict], young_minutes: float, cfg=None, skipped: dict | None = None) -> list[dict]:
    """Shadow rows -> test rows.

    With cfg, each row's result ("ret") is the CURRENT exit rules of Risk (stop, trailing AND the time exit)
    replayed on its price path; the live shadow simulation (sim_net_pct) is kept only for rows without a path.
    With cfg, rows the current rules can't use are left out (counted in `skipped`): price paths that can't be
    trusted (path_problem) and, with [scout] venues = "pumpfun", copycat mints the Filter now blocks for good."""
    rows = []
    r_ = cfg.risk if cfg is not None else None
    pump_only = cfg is not None and getattr(cfg.scout, "venues", "") == "pumpfun"

    def skip(why: str) -> None:
        if skipped is not None:
            skipped[why] = skipped.get(why, 0) + 1
    for r in raw:
        try:
            f = json.loads(r.get("features") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(f, dict) or r.get("sim_net_pct") is None:
            continue
        mint = r.get("mint", "") or ""
        if pump_only and mint and not mint.endswith("pump"):
            skip("copycat")
            continue
        path = None
        if r.get("path"):
            try:
                path = [(float(a), float(b)) for a, b in json.loads(r["path"])]
            except (TypeError, ValueError):
                path = None
        cost = float(r.get("cost_pct") or 2.5)
        ret = clip_ret(float(r["sim_net_pct"]))
        epath = None
        if cfg is not None and path:
            why = path_problem(path)
            if why:
                skip(why)
                continue
            # Entry timing ([entry]): the position starts where the current rule would have bought, or never.
            epath = entry_path(path, entry_settings(cfg.entry))
            ret = 0.0 if not epath else simulate_exit(
                epath, cost, r_.exit_stop_pct or r_.hard_stop_pct, r_.trail_activate_pct, r_.trail_distance_pct,
                float(r_.max_hold_minutes), lock_rule(r_))
        if r.get("score") is not None:
            f.setdefault("score", r["score"])
        rows.append({"ts": r["start_ts"], "mint": mint, "ret": ret,
                     "cost": cost, "decision": r.get("decision", ""), "f": f,
                     "path": path, "epath": epath if (cfg is not None and path) else path,
                     "sit": row_situations(f, young_minutes)})
    rows.sort(key=lambda x: x["ts"])
    return rows


# ── replays ──
def simulate_exit(path: list[tuple[float, float]], cost: float, stop: float, trail_on: float, trail_dist: float,
                  max_hold_min: float = 0, lock: LockRule = OFF) -> float:
    """Net % of the desk exit policy on a recorded price path (raw % moves from the decision price).
    lock: the profit lock (desk/exits.py), same arming/sell levels as Risk."""
    peak, trail, last = 0.0, False, -cost
    touch, armed, level, hist = None, 0.0, 0.0, []
    for sec, raw in path:
        net = raw - cost
        last = net
        if max_hold_min and sec >= max_hold_min * 60:
            return clip_ret(net)
        if net <= stop:
            return clip_ret(net)
        peak = max(peak, net)
        if peak >= trail_on:
            trail = True
        if trail and trail_dist > 0 and 1 + net / 100 <= (1 + peak / 100) * (1 - trail_dist / 100):
            return clip_ret(net)
        if lock.enabled:
            speed = rise_speed(hist, sec, peak, lock.speed_s) if lock.speed_full > 0 else 0.0
            touch, armed = lock_step(lock, peak, sec, touch, armed, cost)
            if armed:
                level = max(level, lock_level(lock, peak, speed))     # the stop only goes up
                if net <= level + lock.buffer:
                    return clip_ret(net)
    return clip_ret(last)


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
    wb = f.get("whale_bonus")                    # Whale checker bonus (score_token), before the caps
    if isinstance(wb, (int, float)):
        s = min(1.0, s + wb)
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
                 sampler, describe, universe_note: str, safe=None) -> None:
        self.agent, self.rows, self.current, self.returns = agent, rows, current, returns
        self.grid, self.sampler, self.describe, self.note = grid, sampler, describe, universe_note
        self.safe = safe      # optional safe(vec, base, idx) -> bool: a variant must also pass it to win


def no_less_safe(vec: list[float], base: list[float], idx: list[int]) -> bool:
    """Exit rules must win AND stay at least as safe (user's ask 2026-11-11: "vincente e abbastanza sicuro"):
    no more losing trades than now, and the worst 10% of trades no more than 1 point worse on average."""
    if sum(vec[i] < 0 for i in idx) > sum(base[i] < 0 for i in idx):
        return False
    k = max(1, len(idx) // 10)
    tail = lambda v: statistics.fmean(sorted(v[i] for i in idx)[:k])   # noqa: E731
    return tail(vec) >= tail(base) - 1.0


def _key(v: dict) -> str:
    return json.dumps(v, sort_keys=True)


def build_spaces(cfg, analyst_rows: list[dict], filter_rows: list[dict], exec_rejects: dict[str, list[str]],
                 min_rows: int) -> list[Space]:
    a, r, f = cfg.analyst, cfg.risk, cfg.filter
    spaces: list[Space] = []
    cur_w = _norm({**BASE_WEIGHTS, **(getattr(a, "weights", None) or {})})

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
        if any(abs(v["weights"][k] - cur_w[k]) > 0.005 for k in cur_w):
            parts.append("[analyst] weights = { " + ", ".join(f"{k} = {v['weights'][k]:.2f}" for k in cur_w) + " }")
        return parts
    spaces.append(Space("Analyst", rows, {"min_score": a.min_score, "weights": cur_w}, a_ret, grid, a_sample,
                        a_desc, note))

    # ── Risk (exit rules replayed on price paths) ──
    with_path = [x for x in analyst_rows if x["path"]]
    rows, note = bought(with_path)
    exit_stop = r.exit_stop_pct or r.hard_stop_pct
    # Paper stop trial (exit stop wider than the hard limit, user's choice): the stop is the user's experiment,
    # the Controller doesn't move it; it still tests trailing and the time exit around it.
    stops = [exit_stop] if exit_stop < r.hard_stop_pct else \
        [s for s in (-6.0, -5.0, -4.0, -3.0) if s >= r.hard_stop_pct]
    trail_on = sorted({max(12.0, r.trail_activate_pct), 12.0, 15.0, 20.0, 25.0, 30.0, 40.0})
    trail_on = [x for x in trail_on if x >= 12.0]
    dists = [3.0, 4.0, 5.0, 6.0, 8.0, 10.0]
    if r.trail_distance_pct <= 0:
        dists = [0.0]     # trailing off (user 2026-11-12): the Controller does not propose turning it back on
    holds = [0.0, 15.0, 30.0, 45.0] if r.loss_stop else [0.0]    # no time exit while there is no loss stop
    cur_r = {"hard_stop_pct": exit_stop, "trail_activate_pct": r.trail_activate_pct,
             "trail_distance_pct": r.trail_distance_pct, "max_hold_minutes": float(r.max_hold_minutes),
             "profit_lock_activate_pct": float(r.profit_lock_activate_pct),
             "profit_lock_fast_activate_pct": float(r.profit_lock_fast_activate_pct),
             "profit_lock_pct": float(r.profit_lock_pct), "profit_lock_keep_pct": float(r.profit_lock_keep_pct),
             "profit_lock_fee_multiple": float(r.profit_lock_fee_multiple),
             "profit_lock_keep_from_pct": float(r.profit_lock_keep_from_pct),
             "profit_lock_keep_fast_pct": float(r.profit_lock_keep_fast_pct),
             "profit_lock_speed_full": float(r.profit_lock_speed_full),
             "profit_lock_keep_low_pct": float(r.profit_lock_keep_low_pct),
             "profit_lock_keep_high_from_pct": float(r.profit_lock_keep_high_from_pct)}
    # Profit lock (user's rule: arm around +5/+6% net, sell around +2/+3% net): the Controller tries other
    # arming / selling levels, keeping the lock always in profit (sell >= +1.5% net, 1+ point under arming).
    lock_on = sorted({float(r.profit_lock_activate_pct), 0.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 12.0})
    lock_on = [x for x in lock_on if x > 0]     # base rule (user 2026-11-11): the stop in profit is never off
    lock_fast = sorted({float(r.profit_lock_fast_activate_pct), 5.0, 6.0, 7.0, 8.0, 10.0, 12.0})
    lock_at = sorted({float(r.profit_lock_pct), 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0})
    # fee rule (user 2026-11-11): the lock also waits until the net profit is this many times the trade's fees
    lock_fee = sorted({float(r.profit_lock_fee_multiple), 0.0, 1.0, 1.5, 2.0, 3.0})
    # keep = share of the peak profit locked (0 = fixed break-even-plus-fees lock, the user's "not always" variant)
    lock_keep = sorted({float(r.profit_lock_keep_pct), 0.0, 30.0, 40.0, 50.0, 60.0, 70.0})
    # Stop that rises with the price (user 2026-11-11: +30% -> +20%, +100% -> +70%, more on a fast rise). While
    # it is on, the Controller tunes it around the user's figures and does not switch the share off.
    rising = r.profit_lock_keep_from_pct > 0
    if rising:
        lock_keep = sorted({float(r.profit_lock_keep_pct), 60.0, 70.0, 80.0})
    lock_from = sorted({float(r.profit_lock_keep_from_pct), 20.0, 30.0, 40.0}) if rising else [0.0]
    lock_kfast = sorted({float(r.profit_lock_keep_fast_pct), 0.0, 80.0, 85.0, 90.0}) if rising \
        else [float(r.profit_lock_keep_fast_pct)]
    lock_speed = sorted({float(r.profit_lock_speed_full), 15.0, 30.0, 60.0}) if rising \
        else [float(r.profit_lock_speed_full)]
    buffer = float(r.profit_lock_sell_buffer_pct)

    def lock_ok(v):
        # User 2026-11-11: never arm early (the first hours swing a lot), so the Controller keeps arming at
        # LOCK_MIN_ON or more; and never turn the lock off (base rule of the engine, user 2026-11-11).
        on = v["profit_lock_activate_pct"]
        kf = v["profit_lock_keep_fast_pct"]
        if on <= 0:
            return False
        return (on >= LOCK_MIN_ON and v["profit_lock_pct"] <= on - 1.0
                           and v["profit_lock_fast_activate_pct"] >= on
                           and (kf == 0 or kf >= v["profit_lock_keep_pct"]))

    def r_lock(v):
        return lock_rule({**v, "profit_lock_fast_minutes": r.profit_lock_fast_minutes,
                          "profit_lock_speed_seconds": r.profit_lock_speed_seconds}, buffer)

    def r_ret(v, rows=rows):
        # exits start where the current entry rule buys (epath); a token the rule doesn't buy counts 0
        lk = r_lock(v)
        return [simulate_exit(x["epath"], x["cost"], v["hard_stop_pct"], v["trail_activate_pct"],
                              v["trail_distance_pct"], v["max_hold_minutes"], lk) if x.get("epath") else 0.0
                for x in rows]
    grid = [{**cur_r, "hard_stop_pct": s} for s in stops] + [{**cur_r, "trail_activate_pct": x} for x in trail_on] \
        + [{**cur_r, "trail_distance_pct": x} for x in dists] + [{**cur_r, "max_hold_minutes": x} for x in holds]
    lock_grid = [{**cur_r, "profit_lock_activate_pct": x} for x in lock_on] \
        + [{**cur_r, "profit_lock_fast_activate_pct": x} for x in lock_fast] \
        + [{**cur_r, "profit_lock_pct": x} for x in lock_at] \
        + [{**cur_r, "profit_lock_keep_pct": x} for x in lock_keep] \
        + [{**cur_r, "profit_lock_fee_multiple": x} for x in lock_fee] \
        + [{**cur_r, "profit_lock_keep_from_pct": x} for x in lock_from] \
        + [{**cur_r, "profit_lock_keep_fast_pct": x} for x in lock_kfast] \
        + [{**cur_r, "profit_lock_speed_full": x} for x in lock_speed] \
        + [{**cur_r, "profit_lock_activate_pct": on, "profit_lock_fast_activate_pct": fast, "profit_lock_pct": at}
           for on, fast, at in ((4.0, 5.0, 2.0), (6.0, 8.0, 3.0), (8.0, 10.0, 4.0), (10.0, 12.0, 5.0))]
    grid += [v for v in lock_grid if lock_ok(v)]

    def r_sample(rng):
        while True:
            v = {**cur_r, "hard_stop_pct": rng.choice(stops), "trail_activate_pct": rng.choice(trail_on),
                 "trail_distance_pct": rng.choice(dists), "max_hold_minutes": rng.choice(holds),
                 "profit_lock_activate_pct": rng.choice(lock_on),
                 "profit_lock_fast_activate_pct": rng.choice(lock_fast), "profit_lock_pct": rng.choice(lock_at),
                 "profit_lock_keep_pct": rng.choice(lock_keep), "profit_lock_fee_multiple": rng.choice(lock_fee),
                 "profit_lock_keep_from_pct": rng.choice(lock_from),
                 "profit_lock_keep_fast_pct": rng.choice(lock_kfast),
                 "profit_lock_speed_full": rng.choice(lock_speed)}
            if lock_ok(v):
                return v

    def r_desc(v):
        return [f"[risk] {k} = {v[k]:g}" for k in cur_r if abs(v[k] - cur_r[k]) > 1e-9]
    spaces.append(Space("Risk", rows, cur_r, r_ret, grid, r_sample, r_desc,
                        note + ", con percorso di prezzo; una variante vince solo se non aumenta i trade in perdita "
                               "né peggiora il 10% peggiore", safe=no_less_safe))

    # ── Risk, wider stops: MEASURE ONLY. The -6% stop is a hard limit, so these never become proposals
    # (within_hard_limits drops them); the report shows them apart, to see whether young volatile tokens would
    # do better with room to move, wider trailing or a time exit. Changing the rule stays the user's call.
    wide = [-8.0, -10.0, -12.0, -15.0, -20.0, -25.0, -30.0]
    w_trail = [12.0, 15.0, 20.0, 30.0, 40.0, 60.0]
    w_dists = [6.0, 8.0, 10.0, 15.0, 20.0]
    w_holds = [0.0, 10.0, 20.0, 30.0, 45.0]
    w_grid = [{**cur_r, "hard_stop_pct": st} for st in wide]
    for st in (-10.0, -15.0, -20.0, -30.0):
        for on, dist in ((20.0, 10.0), (30.0, 15.0), (40.0, 20.0)):
            w_grid.append({**cur_r, "hard_stop_pct": st, "trail_activate_pct": on, "trail_distance_pct": dist})
        for hold in (15.0, 30.0):
            w_grid.append({**cur_r, "hard_stop_pct": st, "max_hold_minutes": hold})

    def w_sample(rng):
        return {**cur_r, "hard_stop_pct": rng.choice(wide), "trail_activate_pct": rng.choice(w_trail),
                "trail_distance_pct": rng.choice(w_dists), "max_hold_minutes": rng.choice(w_holds)}
    spaces.append(Space(WIDE_STOPS, rows, cur_r, r_ret, w_grid, w_sample, r_desc,
                        note + ", con percorso di prezzo; stop più larghi del limite rigido: solo misura"))

    # ── Timing (when to buy an approved token: desk/entry.py), replayed on the same price paths ──
    rows, note = bought(with_path)
    cur_t = entry_settings(cfg.entry)
    stop_now = exit_stop

    def t_ret(v, rows=rows):
        out = []
        for x in rows:
            ep = entry_path(x["path"], v)
            out.append(simulate_exit(ep, x["cost"], stop_now, r.trail_activate_pct, r.trail_distance_pct,
                                     float(r.max_hold_minutes), lock_rule(r)) if ep else 0.0)
        return out
    used = {"now": (), "dip": ("window_minutes", "dip_pct", "fallback"),
            "bounce": ("window_minutes", "dip_pct", "bounce_pct", "fallback"),
            "confirm": ("window_minutes", "confirm_minutes", "confirm_pct")}

    def t_norm(v):
        """Keys a rule doesn't use stay at their current value (no meaningless changes in the proposals)."""
        keep = ("mode",) + used.get(v["mode"], ())
        return {k: (v[k] if k in keep else cur_t[k]) for k in cur_t}
    windows = [3.0, 5.0, 10.0, MAX_WINDOW_MINUTES]
    t_dips = [2.0, 3.0, 5.0, 8.0, 12.0]
    t_bounces = [1.0, 2.0, 4.0]
    t_conf = [(m, c) for m in (2.0, 3.0, 5.0, 10.0) for c in (-3.0, 0.0, 3.0)]
    grid = [{**cur_t, "mode": "now"}]
    for wdw in windows:
        for d in t_dips:
            grid.append({**cur_t, "mode": "dip", "window_minutes": wdw, "dip_pct": d, "fallback": "skip"})
        grid.append({**cur_t, "mode": "dip", "window_minutes": wdw, "dip_pct": 5.0, "fallback": "buy"})
        for d in (3.0, 5.0, 10.0):
            for b in t_bounces:
                grid.append({**cur_t, "mode": "bounce", "window_minutes": wdw, "dip_pct": d, "bounce_pct": b,
                             "fallback": "skip"})
    for m, c_ in t_conf:
        grid.append({**cur_t, "mode": "confirm", "window_minutes": max(m, 5.0), "confirm_minutes": m,
                     "confirm_pct": c_})

    def t_sample(rng):
        mode = rng.choice(("dip", "bounce", "confirm"))
        wdw = rng.choice(windows)
        return t_norm({**cur_t, "mode": mode, "window_minutes": wdw, "dip_pct": rng.choice(t_dips),
                       "bounce_pct": rng.choice(t_bounces),
                       "confirm_minutes": min(wdw, rng.choice((2.0, 3.0, 5.0, 10.0))),
                       "confirm_pct": rng.choice((-3.0, 0.0, 3.0)), "fallback": rng.choice(("skip", "buy"))})
    grid = [t_norm(v) for v in grid]

    def t_desc(v):
        out = []
        for k in cur_t:
            if v[k] != cur_t[k] if isinstance(v[k], str) else abs(v[k] - cur_t[k]) > 1e-9:
                out.append(f'[entry] {k} = "{v[k]}"' if isinstance(v[k], str) else f"[entry] {k} = {v[k]:g}")
        return out
    spaces.append(Space("Timing", rows, cur_t, t_ret, grid, t_sample, t_desc,
                        note + ", con percorso di prezzo: quando comprare dopo l'approvazione"))

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
            if rc != "entry timing":            # measured by the Timing space, on the price path
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
    # Winners saved before a setting existed (e.g. profit_lock_fee_multiple, 2026-11-11) lack its key: they take
    # the current value for it, so the stored memory keeps competing instead of crashing the round.
    def full(v):
        return {**sp.current, **v} if isinstance(v, dict) and isinstance(sp.current, dict) else v
    for v in sp.grid + [sp.sampler(rng) for _ in range(n_random)] + previous:
        v = full(v)
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
            if sp.safe is not None and not sp.safe(vec, base, train):
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
        if sp.safe is not None and not sp.safe(vec, base, valid):
            verdict = "nessun miglioramento (meno sicura sui dati di verifica)"
        elif st["mean"] > 0 and va["n"] >= max(10, min_rows // 3) and va["lo"] > 0:
            verdict = "solida"
        elif st["mean"] > 0 and va["mean"] > 0:
            verdict = "promettente"
        else:
            verdict = "nessun miglioramento"
        results.append({**entry, "variant": variants[k], "current": sp.current,
                        "changes": sp.describe(variants[k]), "train": st,
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
                    previous: dict[str, list[dict]], seed: int | None = None,
                    reviews: list[dict] | None = None) -> dict:
    """One round of tests. Pure CPU, no I/O: safe to run in a worker thread.

    reviews: decisions the Controller applied earlier ({id, agent, since, before, after} variants): each is
    re-checked on the tokens that arrived after it, new setting vs old one on the same tokens."""
    c = cfg.controller
    young = cfg.analyst.young_age_hours * 60
    skipped: dict = {}
    ar = parse_rows(analyst_raw, young, cfg, skipped)
    fr = parse_rows(filter_raw, young, cfg, skipped)
    rng = random.Random(seed if seed is not None else time.time_ns())
    out = {"t": time.time(), "analyst_rows": len(ar), "with_path": sum(1 for x in ar if x["path"]),
           "filter_rows": len(fr), "since": min([x["ts"] for x in ar + fr], default=None),
           "results": [], "tried": 0, "best": {}, "reviews": [], "skipped": skipped}
    spaces = build_spaces(cfg, ar, fr, exec_rejects, c.min_rows)
    by_agent = {sp.agent: sp for sp in spaces}
    for rv in reviews or []:
        sp = by_agent.get(rv["agent"])
        if sp is None:
            continue
        idx = [i for i, x in enumerate(sp.rows) if x["ts"] >= rv["since"]]
        if isinstance(sp.current, dict) and isinstance(rv.get("after"), dict) and isinstance(rv.get("before"), dict):
            rv = {**rv, "after": {**sp.current, **rv["after"]}, "before": {**sp.current, **rv["before"]}}
        try:
            a, b = sp.returns(rv["after"]), sp.returns(rv["before"])
        except (KeyError, TypeError):
            continue                       # the agent's settings space changed shape since: can't compare
        out["reviews"].append({"id": rv["id"], **paired([a[i] - b[i] for i in idx])})
        # what was learned keeps competing: the old and new settings stay among the candidates
        previous = {**previous, sp.agent: list(previous.get(sp.agent, [])) + [rv["before"], rv["after"]]}
    out["entry_trial"] = entry_trial(cfg, by_agent.get("Timing"))
    for sp in spaces:
        res, tried = evaluate_space(sp, rng, c.random_variants, previous.get(sp.agent, []), c.holdout_pct / 100,
                                    c.min_rows)
        out["results"] += res
        out["tried"] += tried
        out["best"][sp.agent] = top_variants(res)
        if not res:
            out["results"].append({"agent": sp.agent, "situation": "all", "n": len(sp.rows), "note": sp.note,
                                   "verdict": f"dati insufficienti ({len(sp.rows)} token, ne servono {c.min_rows})"})
    return out


def entry_trial(cfg, sp: "Space | None") -> dict | None:
    """The user's entry-timing trial ([entry] trial_since): the rule vs buying at once, on the tokens approved since
    the trial started (paired, same tokens). status: in prova (too few tokens yet) | confermata (better, 95% interval
    above 0) | incerta | da rimuovere (worse on average after enough tokens; "smentita" when the interval is all
    below 0)."""
    from .quant import parse_since
    since_s = getattr(cfg.entry, "trial_since", "")
    if not since_s or sp is None:
        return None
    since = parse_since(since_s)
    idx = [i for i, x in enumerate(sp.rows) if x["ts"] >= since]
    rule, now_ = sp.returns(sp.current), sp.returns({**sp.current, "mode": "now"})
    st = paired([rule[i] - now_[i] for i in idx])
    need = cfg.controller.review_min_rows
    if st["n"] < need:
        status = "in prova"
    elif st["hi"] < 0:
        status = "da rimuovere (smentita)"
    elif st["mean"] < 0:
        status = "da rimuovere"
    elif st["lo"] > 0:
        status = "confermata"
    else:
        status = "incerta"
    return {**st, "need": need, "since": since, "status": status, "rule": dict(sp.current),
            "taken": sum(1 for i in idx if rule[i] != 0)}


def entry_rule_text(r: dict) -> str:
    fb = ", altrimenti nessun acquisto" if r.get("fallback") == "skip" else ", altrimenti compra a fine finestra"
    return {"dip": f"ribasso di {r['dip_pct']:g}% entro {r['window_minutes']:g} min" + fb,
            "bounce": f"ribasso di {r['dip_pct']:g}% poi rimbalzo di {r['bounce_pct']:g}% entro "
                      f"{r['window_minutes']:g} min" + fb,
            "confirm": f"prezzo >= {r['confirm_pct']:+g}% dopo {r['confirm_minutes']:g} min"}.get(r["mode"], "subito")


def entry_trial_section(t: dict | None) -> list[str]:
    if not t:
        return []
    out = ["## Prova del momento d'acquisto (scelta dell'utente)",
           f"- regola: {entry_rule_text(t['rule'])}, attiva dal {datetime.fromtimestamp(t['since'], timezone.utc):%Y-%m-%d %H:%M} UTC"]
    if not t["n"]:
        out.append(f"- nessun token approvato da allora (ne servono {t['need']} per giudicarla)")
    else:
        out.append(f"- {t['n']} token approvati da allora ({t['taken']} comprati con la regola): "
                   f"{t['mean']:+.2f} punti % per token rispetto a comprare subito, intervallo 95% "
                   f"[{t['lo']:+.2f}, {t['hi']:+.2f}]")
    verdict = {"in prova": f"IN PROVA: servono almeno {t['need']} token per giudicarla",
               "confermata": "CONFERMATA: meglio che comprare subito",
               "incerta": "INCERTA: per ora non peggiora, i dati non bastano a confermarla",
               "da rimuovere": "DA RIMUOVERE: con i dati nuovi rende meno che comprare subito. Per toglierla: "
                               "[entry] mode = \"now\" e trial_since = \"\" in config.toml",
               "da rimuovere (smentita)": "DA RIMUOVERE, SMENTITA: rende meno che comprare subito con intervallo "
                                          "tutto sotto zero. Per toglierla: [entry] mode = \"now\" e "
                                          "trial_since = \"\" in config.toml"}[t["status"]]
    return out + [f"- {verdict}", ""]


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
        if (sec, key) == ("analyst", "weights"):
            continue                        # score weights: no hard limit (validated when config.toml is loaded)
        if sec == "entry" and getattr(cfg.entry, "trial_since", ""):
            return False                    # entry-timing trial running: the rule is the user's choice
        if (sec, key) == ("risk", "hard_stop_pct") and cfg.risk.exit_stop_pct and \
                cfg.risk.exit_stop_pct < cfg.risk.hard_stop_pct:
            return False                    # paper stop trial running: the stop is the user's experiment
        obj = getattr(test, sec, None)
        if obj is None or not hasattr(obj, key):
            return False
        cur = getattr(obj, key)
        if isinstance(cur, str):
            v = val.strip().strip('"').strip("'")     # '[entry] mode = "dip"' -> dip
        else:
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
        if r.get("verdict") not in ("solida", "promettente") or not r.get("changes") or r["agent"] == WIDE_STOPS:
            continue
        if not within_hard_limits(cfg, r["changes"]):
            continue
        cands.append({"agent": r["agent"], "situation": r["situation"], "label": SITUATIONS[r["situation"]],
                      "changes": r["changes"], "verdict": r["verdict"], "gain": r["valid"]["mean"],
                      "lo": r["valid"]["lo"], "hi": r["valid"]["hi"], "n": r["n"], "valid_n": r["valid"]["n"],
                      "variant": r.get("variant"), "current": r.get("current")})
    cands.sort(key=lambda p: (p["verdict"] != "solida", p["situation"] != "all", -p["gain"]))
    groups: dict[tuple, dict] = {}
    for p in cands:
        k = (p["agent"], tuple(p["changes"]))
        if k in groups:
            groups[k]["also"].append(p["label"])
        else:
            groups[k] = {**p, "also": []}
    return list(groups.values())


def wide_stop_section(exp: dict) -> list[str]:
    """Stops wider than -6% (with wider trailing / time exits): what they would have done, never a proposal."""
    rs = [r for r in exp.get("results", []) if r["agent"] == WIDE_STOPS]
    out = ["## Stop più larghi del -6% (solo misura, non proposte)",
           "Lo stop a -6% è un limite rigido: queste varianti non vengono mai proposte. Servono a capire se i "
           "token giovani e volatili renderebbero di più lasciando più spazio al prezzo. Se una risulta SOLIDA "
           "per più cicli, la scelta di cambiare la regola resta tua."]
    shown = 0
    for sit in ("young", "all", "small_pool", "big_pool", "old"):
        r = next((x for x in rs if x["situation"] == sit), None)
        if r is None:
            continue
        head = f"- {SITUATIONS[sit]} ({r['n']} token)"
        if "variant" not in r:
            out.append(f"{head}: {r['verdict']}")
        else:
            out.append(f"{head}: attuale {r['current_avg']:+.2f}% per token | migliore: "
                       f"{'; '.join(r['changes']) or 'nessuna modifica'} -> {r['valid']['mean']:+.2f} "
                       f"[{r['valid']['lo']:+.2f}, {r['valid']['hi']:+.2f}] in verifica — {r['verdict'].upper()}")
        shown += 1
    if not shown:
        out.append("- dati insufficienti per ora")
    return out + [""]


def build_report(health: list[dict], exp: dict | None, cfg, trials_total: int, rounds: int,
                 decisions: list[str] | None = None, props: list[dict] | None = None) -> str:
    t = time.time()
    lines = [f"# Controllore — report del {datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M} UTC", "",
             "Il Controllore verifica gli agenti e prova configurazioni diverse sui dati reali registrati dal "
             "Logger. Non fa trade e non applica nulla da solo: ogni modifica che farebbe compare tra le Proposte, "
             "sempre dentro i limiti rigidi, e decidi tu se applicarla (regola base, 2026-11-11).", "",
             "## Comportamento degli agenti"]
    for f in health:
        lines.append(f"- [{LEVEL[f['level']]}] {f['agent']} — {f['check']}: {f['detail']}")
    lines.append("")
    lines += decisions or []
    lines += entry_trial_section((exp or {}).get("entry_trial"))
    if not exp:
        lines.append("Primo ciclo di test non ancora eseguito.")
        return "\n".join(lines)
    since = exp.get("since")
    lines += ["## Dati usati",
              f"- {exp['analyst_rows']} token valutati dall'Analyst ({exp['with_path']} con percorso di prezzo), "
              f"{exp['filter_rows']} token valutati dal Filter, ciascuno seguito per 60 minuti"
              + (f", dal {datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d %H:%M} UTC" if since else ""),
              *([f"- esclusi dai test: " + ", ".join(f"{n} {SKIP_TEXT.get(k, k)}" for k, n in sorted(
                  exp["skipped"].items()))] if exp.get("skipped") else []),
              f"- ogni token è misurato con le regole di uscita attuali di Risk (stop, trailing, uscita a tempo e stop a profitto) "
              f"rigiocate sul suo percorso di prezzo",
              f"- {exp['tried']} configurazioni provate in questo ciclo, {trials_total} in totale in {rounds} cicli",
              f"- scelta sul {100 - cfg.controller.holdout_pct:.0f}% più vecchio dei dati, verifica sul "
              f"{cfg.controller.holdout_pct:.0f}% più recente", ""]
    if props is None:
        props = proposals(exp, cfg)
    lines.append("## Proposte (da valutare: il Controllore non le applica)")
    if not props:
        lines.append("Nessuna proposta per ora: nessuna variante batte la configurazione attuale anche sui dati di "
                     "verifica. Più dati arrivano, più i test diventano precisi.")
    for p in props:
        lines.append(f"- {p['agent']} | {p['label']} | {'; '.join(p['changes'])}")
        if p["also"]:
            lines.append(f"    vince anche in: {', '.join(p['also'])}")
        lines.append(f"    guadagno stimato {p['gain']:+.2f} punti % per token valutato, intervallo 95% "
                     f"[{p['lo']:+.2f}, {p['hi']:+.2f}] su {p['valid_n']} token di verifica — {p['verdict'].upper()}")
    if props:
        lines.append("Per applicarne una: chiedilo a Claude o scrivi la riga in config.toml (attenzione: una chiave "
                     f"presente in data/controller_overrides_{cfg.mode}.json vince su config.toml).")
    lines.append("")
    lines += wide_stop_section(exp)
    lines.append("## Dettaglio per agente e situazione")
    for agent in ("Scout", "Filter", "Analyst", "Timing", "Executor", "Risk", WIDE_STOPS):
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


def profit_lock_section(trades: list[dict], cfg) -> list[str]:
    """How the profit lock worked on our real trades (exit records stored with every trade since 2026-11-11,
    kept in trades_archive on a paper reset). Read together with the Risk replays, which test other levels."""
    r = cfg.risk
    out = ["## Stop a profitto (blocco del guadagno)",
           f"Regola attuale: dopo +{r.profit_lock_activate_pct:g}% netto (+{r.profit_lock_fast_activate_pct:g}% se "
           f"raggiunto entro {r.profit_lock_fast_minutes:g} min dall'acquisto) lo stop sale a "
           f"+{r.profit_lock_pct:g}% netto"
           + (f"; da un picco di +{r.profit_lock_keep_from_pct:g}% lo stop sale col prezzo e tiene il "
              f"{r.profit_lock_keep_pct:g}% del picco netto"
              + (f", fino al {r.profit_lock_keep_fast_pct:g}% quando il picco sale di {r.profit_lock_speed_full:g} "
                 f"punti al minuto o più" if r.profit_lock_keep_fast_pct > r.profit_lock_keep_pct
                 and r.profit_lock_speed_full > 0 else "") + " (non scende mai)"
              if r.profit_lock_keep_from_pct > 0 and r.profit_lock_keep_pct > 0 else
              f", o il {r.profit_lock_keep_pct:g}% del picco netto se più alto (sale col picco; 0 = stop fisso a "
              f"pareggio con commissioni)")
           + f"; ogni livello è alzato dei costi di vendita (minimo "
             f"{r.profit_lock_sell_buffer_pct:g}%, o lo slittamento misurato se maggiore)."
           + (f" Si arma solo quando il guadagno netto è almeno {r.profit_lock_fee_multiple:g} volte le commissioni "
              f"andata/ritorno del trade, se questo è più alto della soglia."
              if r.profit_lock_fee_multiple > 0 else "")
           if r.profit_lock_activate_pct > 0 else "Regola disattivata (profit_lock_activate_pct = 0)."]
    recs = []
    for t in trades:
        d = t.get("data")
        if isinstance(d, str):
            try:
                d = json.loads(d or "{}")
            except ValueError:
                continue
        ex = (d or {}).get("exit") if isinstance(d, dict) else None
        if isinstance(ex, dict) and ex.get("realized_pnl_pct") is not None:
            recs.append(ex)
    if not recs:
        return out + ["- nessun trade registrato con la nuova regola per ora", ""]
    armed = [x for x in recs if (x.get("lock") or {}).get("armed")]
    by = {}
    for x in armed:
        by.setdefault(x.get("kind", "?"), []).append(float(x["realized_pnl_pct"]))
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0   # noqa: E731
    out.append(f"- {len(recs)} trade registrati, stop a profitto armato su {len(armed)}")
    for kind, xs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        out.append(f"    - chiusi da {kind}: {len(xs)}, risultato medio {avg(xs):+.2f}% "
                   f"(peggiore {min(xs):+.2f}%, in perdita {sum(v <= 0 for v in xs)})")
    locks = [x for x in armed if x.get("kind") == "PROFIT_LOCK" and x.get("trigger_pnl_pct") is not None]
    if locks:
        slips = sorted(float(x["trigger_pnl_pct"]) - float(x["realized_pnl_pct"]) for x in locks)
        bufs = [float((x.get("lock") or {}).get("buffer_pct") or 0) for x in locks]
        out.append(f"- vendite dello stop a profitto: slittamento mediano {slips[len(slips) // 2]:.2f}% tra il "
                   f"valore che le ha fatte scattare e l'incasso reale (margine usato {avg(bufs):.2f}%)")
        if sum(float(x["realized_pnl_pct"]) <= 0 for x in locks) * 4 > len(locks):
            out.append("    - ATTENZIONE: più di 1 vendita su 4 non è rimasta in profitto: alzare "
                       "[risk] profit_lock_pct o profit_lock_sell_buffer_pct")
    near = [x for x in recs if not (x.get("lock") or {}).get("armed")
            and float(x.get("peak_pnl_pct") or 0) >= max(1.0, r.profit_lock_activate_pct - 2)
            and float(x["realized_pnl_pct"]) < 0]
    if near:
        out.append(f"- {len(near)} trade sono saliti vicino alla soglia (picco ≥ +{max(1.0, r.profit_lock_activate_pct - 2):g}%) "
                   f"senza armare lo stop e poi hanno chiuso in perdita: le prove su soglie più basse sono nella "
                   f"sezione Risk")
    out.append("- le soglie alternative (armare/vendere più in alto o più in basso) sono provate sui percorsi di "
               "prezzo nella sezione Risk e compaiono tra le Proposte quando battono quelle attuali")
    return out + [""]
