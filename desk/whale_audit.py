"""How precise is the Whale checker? (user 2026-11-12) — measured by the Controller as the data comes in.

The Whale checker (desk/agents/whale.py, desk/smartwallets.py) tells the Analyst how many "smart wallets" hold a
token, and the Analyst adds a bonus for them once the data is sufficient. This module checks, on the desk's own
records, whether that information is right:

  A. 60-MINUTE RESULT. Tokens the Analyst scored with a ready Whale checker report, split by "smart holders
     present" vs "none": average result with the CURRENT exit rules replayed on the price path (the same number the
     Controller's tests use), share of winners, and the difference between the two groups with a 95% interval.
  B. TOKENS THE BONUS PROMOTED. Tokens that passed the threshold only thanks to the bonus (score - bonus < threshold
     <= score): what they would have made. This is the direct test of the rule.
  C. $200k PRECISION. Of the tokens where the Whale checker saw smart holders (any stage), how many reached the
     win line ($200k real market cap), against the same share for every token the Analyst scored (the base rate).
  D. WALLETS. The smart wallets seen on >= 3 decided tokens, best and worst precision.

Only proposals come out of it (the Controller never applies anything): switch the bonus off when the promoted
tokens lose with a 95% interval below zero, raise it when they win with an interval above zero and the "smart
holders present" group beats the other one too.
"""
from __future__ import annotations

import math
import re
import sqlite3
import statistics
import time

_MC = re.compile(r"market cap \$([\d,]+)")
_LIQ = re.compile(r"liquidity: ([\d.]+) vs")
MIN_GROUP = 15            # fewest tokens a group needs before its numbers are judged


def stats(xs: list[float], z: float = 1.96) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0, "mean": 0.0, "lo": 0.0, "hi": 0.0, "win": 0.0}
    m = statistics.fmean(xs)
    h = z * statistics.stdev(xs) / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": m, "lo": m - h, "hi": m + h, "win": sum(x > 0 for x in xs) / n}


def diff(a: list[float], b: list[float], z: float = 1.96) -> dict:
    """Mean of a minus mean of b, with a Welch 95% interval."""
    if len(a) < 2 or len(b) < 2:
        return {"mean": 0.0, "lo": 0.0, "hi": 0.0, "ok": False}
    m = statistics.fmean(a) - statistics.fmean(b)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return {"mean": m, "lo": m - z * se, "hi": m + z * se, "ok": True}


def load(db_path, mints: set[str]) -> tuple[list[dict], dict[str, dict]]:
    """(sw_hits rows, mint -> {"peak", "first_ts"}) read-only. Peaks: the Whale checker's tracker (sw_tokens) and the
    Filter's own market-cap readings backed by real liquidity."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    hits: list[dict] = []
    out: dict[str, dict] = {}
    try:
        try:
            hits = [dict(r) for r in db.execute("SELECT mint, wallet, ts, stage FROM sw_hits")]
            for r in db.execute("SELECT mint, peak_mcap, create_ts, scanned_ts FROM sw_tokens"):
                out[r["mint"]] = {"peak": float(r["peak_mcap"] or 0),
                                  "first_ts": r["create_ts"] or r["scanned_ts"]}
        except sqlite3.OperationalError:          # Whale checker never started: no tables yet
            return [], {}
        want = sorted(mints | {h["mint"] for h in hits})
        for i in range(0, len(want), 400):
            chunk = want[i: i + 400]
            q = ("SELECT mint, data FROM decisions WHERE mint IN (%s) AND data LIKE '%%market cap $%%'"
                 % ",".join("?" * len(chunk)))
            for r in db.execute(q, chunk):
                m, liq = _MC.search(r["data"] or ""), _LIQ.search(r["data"] or "")
                if not m or not liq:
                    continue
                mc, lq = float(m.group(1).replace(",", "")), float(liq.group(1))
                if lq >= 10_000 and mc / lq <= 25:
                    d = out.setdefault(r["mint"], {"peak": 0.0, "first_ts": None})
                    d["peak"] = max(d["peak"], mc)
    finally:
        db.close()
    return hits, out


def audit(rows: list[dict], hits: list[dict], tokens: dict[str, dict], min_score: float, win_mcap: float,
          decide_hours: float, now: float | None = None) -> dict:
    """rows: the Controller's parsed Analyst rows (ret = current exit rules; f = features at scoring time)."""
    now = now or time.time()
    ready = [r for r in rows if r["f"].get("whale_ready") is True]
    with_ = [r["ret"] for r in ready if (r["f"].get("whale_holders") or 0) > 0]
    without = [r["ret"] for r in ready if (r["f"].get("whale_holders") or 0) == 0]
    promoted = [r["ret"] for r in ready if (r["f"].get("whale_bonus") or 0) > 0
                and isinstance(r["f"].get("score"), (int, float))
                and r["f"]["score"] >= min_score > r["f"]["score"] - r["f"]["whale_bonus"]]

    first_seen: dict[str, float] = {}
    for r in rows:
        first_seen[r["mint"]] = min(first_seen.get(r["mint"], r["ts"]), r["ts"])
    for h in hits:
        first_seen[h["mint"]] = min(first_seen.get(h["mint"], h["ts"]), h["ts"])

    def outcome(mint: str) -> bool | None:
        """True = reached the win line, False = did not within decide_hours, None = too early to say."""
        t = tokens.get(mint) or {}
        if float(t.get("peak") or 0) >= win_mcap:
            return True
        start = t.get("first_ts") or first_seen.get(mint)
        if start and now - float(start) >= decide_hours * 3600:
            return False
        return None

    hit_mints = {h["mint"] for h in hits}
    hit_out = [o for o in (outcome(m) for m in hit_mints) if o is not None]
    base_out = [o for o in (outcome(m) for m in {r["mint"] for r in rows}) if o is not None]
    per_wallet: dict[str, list[bool]] = {}
    for h in hits:
        o = outcome(h["mint"])
        if o is not None:
            per_wallet.setdefault(h["wallet"], []).append(o)
    wallets = sorted(((w, sum(v), len(v)) for w, v in per_wallet.items() if len(v) >= 3),
                     key=lambda x: (-x[1] / x[2], -x[2]))
    prec = sum(hit_out) / len(hit_out) if hit_out else None
    base = sum(base_out) / len(base_out) if base_out else None
    return {"ready_rows": len(ready), "with": stats(with_), "without": stats(without),
            "diff": diff(with_, without), "promoted": stats(promoted),
            "hits": len(hits), "hit_tokens": len(hit_mints), "hit_decided": len(hit_out),
            "precision": prec, "base_decided": len(base_out), "base_rate": base,
            "lift": (prec / base) if prec is not None and base else None,
            "wallets": wallets, "t": now}


def proposals(a: dict | None, cfg) -> list[dict]:
    """Proposals only (the user decides): bonus off when it demonstrably loses, higher when it demonstrably wins."""
    if not a:
        return []
    an, p, d = cfg.analyst, a["promoted"], a["diff"]
    out = []
    base = {"agent": "Analyst", "situation": "all", "kind": "whale", "n": p["n"], "valid_n": p["n"], "also": [],
            "gain": 0.0, "lo": 0.0, "hi": 0.0}
    if an.whale_bonus_per_wallet > 0 and p["n"] >= MIN_GROUP and p["hi"] < 0:
        out.append({**base, "label": "Whale checker: i token promossi dal bonus perdono",
                    "changes": ["[analyst] whale_bonus_per_wallet = 0"], "verdict": "solida",
                    "gain": -p["mean"], "lo": -p["hi"], "hi": -p["lo"]})
    elif an.whale_bonus_per_wallet > 0 and p["n"] >= MIN_GROUP and p["lo"] > 0 and d["ok"] and d["lo"] > 0 \
            and a["with"]["n"] >= MIN_GROUP and a["without"]["n"] >= MIN_GROUP:
        per, mx = round(an.whale_bonus_per_wallet * 1.5, 3), round(an.whale_bonus_max * 1.5, 3)
        out.append({**base, "label": "Whale checker: i token con wallet bravi rendono di più",
                    "changes": [f"[analyst] whale_bonus_per_wallet = {per:g}", f"[analyst] whale_bonus_max = {mx:g}"],
                    "verdict": "promettente", "gain": d["mean"], "lo": d["lo"], "hi": d["hi"]})
    return out


def report_section(a: dict | None, cfg, readiness: dict | None = None) -> str:
    sw, an = cfg.smart_wallets, cfg.analyst
    lines = ["", "", "## Whale checker — precisione delle informazioni",
             f"Regola: con dati sufficienti (almeno {sw.ready_min_decided_tokens} token decisi, "
             f"{sw.ready_min_winners} vincenti, {sw.ready_min_smart_wallets} wallet bravi) ogni wallet bravo presente "
             f"aggiunge +{an.whale_bonus_per_wallet:g} al punteggio dell'Analyst, al massimo +{an.whale_bonus_max:g}; "
             f"senza dati sufficienti l'informazione è ignorata."]
    if readiness:
        lines.append(f"- stato: {'dati sufficienti' if readiness.get('ready') else 'dati NON sufficienti'} "
                     f"({readiness.get('why', '')})")
    if not a:
        return "\n".join(lines + ["- nessun dato ancora"])
    pct = lambda x: f"{100 * x:.0f}%" if x is not None else "n/d"      # noqa: E731

    def grp(name: str, s: dict) -> str:
        if not s["n"]:
            return f"- {name}: nessun token ancora"
        tail = "" if s["n"] >= MIN_GROUP else f" (pochi dati: servono almeno {MIN_GROUP} token)"
        return (f"- {name}: {s['n']} token, risultato medio {s['mean']:+.2f}% [{s['lo']:+.2f}, {s['hi']:+.2f}], "
                f"in guadagno {pct(s['win'])}{tail}")

    lines += [f"### A. Risultato a 60 minuti (regole di uscita attuali), {a['ready_rows']} token valutati con dati "
              f"sufficienti",
              grp("con wallet bravi presenti", a["with"]), grp("senza wallet bravi", a["without"])]
    d = a["diff"]
    if d["ok"] and a["with"]["n"] >= MIN_GROUP and a["without"]["n"] >= MIN_GROUP:
        verdict = "MEGLIO" if d["lo"] > 0 else "PEGGIO" if d["hi"] < 0 else "nessuna differenza certa per ora"
        lines.append(f"- differenza: {d['mean']:+.2f} punti % [{d['lo']:+.2f}, {d['hi']:+.2f}] -> {verdict}")
    lines += ["### B. Token passati alla soglia solo grazie al bonus", grp("promossi dal bonus", a["promoted"])]
    lines += [f"### C. Precisione sul traguardo ${cfg.smart_wallets.win_mcap_usd / 1000:.0f}k",
              f"- {a['hits']} segnalazioni su {a['hit_tokens']} token, {a['hit_decided']} già decisi: "
              f"{pct(a['precision'])} hanno raggiunto il traguardo",
              f"- base: {pct(a['base_rate'])} dei {a['base_decided']} token valutati dall'Analyst e già decisi"
              + (f" -> il segnale è {a['lift']:.1f} volte la base" if a["lift"] is not None else "")]
    if a["wallets"]:
        lines.append("### D. Wallet con almeno 3 token decisi")
        ws = a["wallets"]
        for w, ok, n in (ws[:5] + ([] if len(ws) <= 10 else [("…", 0, 0)]) + ws[-5:] if len(ws) > 10 else ws):
            lines.append(f"- {w[:8]}: {ok}/{n} al traguardo" if n else "- …")
    props = proposals(a, cfg)
    lines.append("- proposta: " + ("; ".join(p["changes"][0] for p in props) + " (vedi Proposte)" if props else
                                   "nessuna per ora (servono dati più chiari)"))
    return "\n".join(lines)

