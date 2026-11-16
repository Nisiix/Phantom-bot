"""Controller report sections on the real horizon (desk/realwatch.py, user 2026-11-16). Read-only, measurement.

  * how the followed tokens really ended (reached +30% net before -50%, or not), approved vs not approved;
  * old and big tokens (point 7 of the 2026-11-16 review): share reaching +30% by age and by market cap;
  * what the Analyst's components say about reaching +30% (point 8): top third vs bottom third of each component.
"""
from __future__ import annotations

import json
import statistics

MIN_GROUP = 15
COMPONENTS = {"c_liquidity": "liquidità", "c_turnover": "rotazione", "c_buy_pressure": "pressione d'acquisto",
              "c_momentum": "momento", "c_holders": "holder", "c_hygiene": "pulizia", "c_website": "sito web",
              "c_maturity": "maturità"}
AGE_BUCKETS = ((0, 60, "meno di 1 ora"), (60, 180, "1-3 ore"), (180, 720, "3-12 ore"), (720, 1e12, "oltre 12 ore"))
MCAP_BUCKETS = ((0, 50e3, "sotto $50k"), (50e3, 100e3, "$50k-100k"), (100e3, 250e3, "$100k-250k"),
                (250e3, 1e15, "oltre $250k"))


def judged(raw: list[dict], cfg) -> list[dict]:
    """Closed realwatch rows -> {f, label, ret, score}, judged by the CURRENT rules on their whole path."""
    from .realwatch import judge
    out = []
    for r in raw:
        try:
            path = [tuple(p) for p in json.loads(r.get("path") or "[]")]
            f = json.loads(r.get("features") or "{}") or {}
        except (TypeError, ValueError):
            continue
        if not path or max(v for _, v in path) > 5000:
            continue
        mint = str(r.get("mint") or "")
        if getattr(cfg.scout, "venues", "") == "pumpfun" and mint and not mint.endswith("pump"):
            continue
        res = judge(path, float(r.get("cost_pct") or 2.5), cfg)
        out.append({"f": f, "label": res["label"], "ret": res["ret_net"], "score": r.get("score"),
                    "stage": r.get("stage"), "ts": r.get("start_ts") or 0})
    return out


def _cell(rows: list[dict]) -> str:
    n = len(rows)
    if not n:
        return "nessun token"
    up = sum(r["label"] == "up" for r in rows)
    down = sum(r["label"] == "down" for r in rows)
    avg = statistics.fmean(r["ret"] for r in rows)
    txt = (f"{n} token: arrivati a +30% {up} ({100 * up / n:.0f}%), scesi a -50% prima {down} ({100 * down / n:.0f}%), "
           f"risultato medio con le uscite attuali {avg:+.1f}%")
    return txt + ("" if n >= MIN_GROUP else f" (pochi dati: servono almeno {MIN_GROUP} token)")


def section(analyst: list[dict], grad: list[dict], cfg) -> str:
    rw = cfg.realwatch
    up_line = cfg.risk.profit_lock_activate_pct
    lines = ["", "", f"## Orizzonte reale ({rw.hours:g} ore): come finiscono davvero i token",
             f"Senza stop in perdita una posizione resta aperta finché lo stop a profitto non vende, anche per giorni. "
             f"Ogni token valutato dall'Analyst e ogni token comprabile a fine attesa dopo la graduazione viene seguito "
             f"per {rw.hours:g} ore. Domanda: arriva a +{up_line:g}% netto (lo stop a profitto si arma) prima di scendere "
             f"a {rw.down_pct:g}%? Solo misura: nessuna regola cambia da sola."]
    if not analyst and not grad:
        return "\n".join(lines + [f"- nessun token seguito fino in fondo ancora: i primi arrivano {rw.hours:g} ore "
                                  f"dopo l'avvio"])
    ms = cfg.analyst.min_score
    appr = [r for r in analyst if (r["score"] or 0) >= ms]
    rej = [r for r in analyst if (r["score"] or 0) < ms]
    lines += [f"- approvati dall'Analyst (punteggio >= {ms:g}): {_cell(appr)}",
              f"- sotto soglia: {_cell(rej)}",
              f"- tutti i token comprabili a fine attesa dopo la graduazione: {_cell(grad)}"]

    # point 7: age and size
    pool = analyst + grad
    lines.append("### Token vecchi e grandi (approvati + fine attesa)")
    for name, key, buckets in (("età", "age_min", AGE_BUCKETS), ("market cap", "mcap", MCAP_BUCKETS)):
        for lo, hi, label in buckets:
            g = [r for r in pool if isinstance(r["f"].get(key), (int, float)) and lo <= r["f"][key] < hi]
            if g:
                lines.append(f"- {name} {label}: {_cell(g)}")
    lines.append("- i tetti più bassi (market cap, età) sono provati come varianti nella sezione "
                 "«Filter (età e dimensione)» e compaiono tra le Proposte solo se battono quelli attuali")

    # point 8: what the Analyst's components say
    rows = [r for r in analyst if any(isinstance(r["f"].get(k), (int, float)) for k in COMPONENTS)]
    lines.append("### Analyst: quali componenti del punteggio anticipano il +30%")
    if len(rows) < 2 * MIN_GROUP:
        lines.append(f"- servono almeno {2 * MIN_GROUP} token valutati e seguiti fino in fondo (ora {len(rows)})")
    else:
        w = {**_weights(cfg)}
        for k, name in COMPONENTS.items():
            g = sorted((r for r in rows if isinstance(r["f"].get(k), (int, float))), key=lambda r: r["f"][k])
            if len(g) < 2 * MIN_GROUP:
                continue
            third = len(g) // 3
            lo, hi = g[:third], g[-third:]
            sh = lambda xs: 100 * sum(r["label"] == "up" for r in xs) / len(xs)   # noqa: E731
            d = sh(hi) - sh(lo)
            lines.append(f"- {name} (peso {100 * w.get(k[2:], 0):.0f}%): terzo più alto {sh(hi):.0f}% arrivati a +30%, "
                         f"terzo più basso {sh(lo):.0f}% ({d:+.0f} punti)"
                         + (" — non anticipa il +30%" if d <= 0 else ""))
        lines.append("- i pesi alternativi sono provati nella sezione Analyst su questo stesso esito, quando "
                     "l'orizzonte reale è attivo; nessun peso cambia da solo")
    return "\n".join(lines)


def _weights(cfg) -> dict:
    try:
        from .agents.analyst import WEIGHTS
        w = {**WEIGHTS, **(cfg.analyst.weights or {})}
        tot = sum(w.values()) or 1.0
        return {k: v / tot for k, v in w.items()}
    except Exception:  # noqa: BLE001
        return {}
