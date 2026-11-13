"""How precise is the Whale checker? (user 2026-11-12) — measured by the Controller as the data comes in.

The Whale checker (desk/agents/whale.py, desk/smartwallets.py) tells the Analyst how many "smart wallets" hold a
token, and the Analyst adds a bonus for them once the data is sufficient. This module checks, on the desk's own
records, whether that information is right:

  A. 60-MINUTE RESULT. Tokens the Analyst scored with a ready Whale checker report, split by "smart holders
     present" vs "none": average result with the CURRENT exit rules replayed on the price path (the same number the
     Controller's tests use), share of winners, and the difference between the two groups with a 95% interval.
  B. TOKENS THE BONUS PROMOTED. Tokens that passed the threshold only thanks to the bonus (score - bonus < threshold
     <= score): what they would have made. This is the direct test of the rule.
  C. WIN-LINE PRECISION. Of the tokens where the Whale checker saw smart holders (any stage), how many reached the
     win line ([smart_wallets] win_mcap_usd, $100k real market cap), against the same share for every token the Analyst scored (the base rate).
  D. WALLETS. The smart wallets seen on >= 3 decided tokens, best and worst precision.

HARVEST (user 2026-11-12): is the Whale checker collecting the addresses in the best, most efficient way? From its
own counters (efficiency(): tokens per hour, seconds / RPC calls / pages per token, tokens too long to read and the
calls wasted on them, backlog, reports to the Analyst, buy prices read) and from the database (harvest(): how many
of the buyers read are usable - not bundle, not relayed, not dust, not the creator -, how many of the tokens the
Analyst scored had been read, how many smart wallets have a known funder and balance). health checks flag the
problems; harvest_proposals() suggests [smart_wallets] settings (read deeper, read fewer trades, read more trades).

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
            # the Analyst's hits: counted with every safeguard (insiders, self-proof, sellers, one per operator)
            hits = [dict(r) for r in db.execute("SELECT mint, wallet, ts, stage FROM sw_hits WHERE stage='analyst'")]
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
    promoted = [r["ret"] for r in ready if isinstance(r["f"].get("whale_free_score"), (int, float))
                and isinstance(r["f"].get("score"), (int, float))
                and r["f"]["score"] >= min_score > r["f"]["whale_free_score"]]

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
    """Proposals only (the user decides): switch the smart-wallet weight off when the tokens it carried over the
    threshold demonstrably lose. Its value (30%, whale_weight) is the user's rule: never proposed higher or lower."""
    if not a:
        return []
    p = a["promoted"]
    if cfg.analyst.whale_weight > 0 and p["n"] >= MIN_GROUP and p["hi"] < 0:
        return [{"agent": "Analyst", "situation": "all", "kind": "whale", "n": p["n"], "valid_n": p["n"], "also": [],
                 "label": "Whale checker: i token approvati grazie ai wallet bravi perdono",
                 "changes": ["[analyst] whale_weight = 0"], "verdict": "solida",
                 "gain": -p["mean"], "lo": -p["hi"], "hi": -p["lo"]}]
    return []


def report_section(a: dict | None, cfg, readiness: dict | None = None) -> str:
    sw, an = cfg.smart_wallets, cfg.analyst
    lines = ["", "", "## Whale checker — precisione delle informazioni",
             f"Regola: con dati sufficienti (almeno {sw.ready_min_decided_tokens} token decisi, "
             f"{sw.ready_min_winners} vincenti, {sw.ready_min_smart_wallets} wallet bravi, prova fuori campione) "
             f"un wallet bravo presente vale il {an.whale_weight:.0%} del punteggio dell'Analyst e l'Executor compra "
             f"entro +{sw.max_entry_premium_pct:g}% dal prezzo d'acquisto dei wallet bravi (altrimenti aspetta fino "
             f"a {sw.whale_wait_minutes:g} min, e scarta il token se il wallet vende); senza dati sufficienti "
             f"l'informazione è ignorata."]
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
    lines += ["### B. Token approvati solo grazie ai wallet bravi", grp("approvati grazie ai wallet bravi", a["promoted"])]
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



# ═════════════════════════════════ harvest: how well the addresses are collected ═════════════════════════════════
def harvest(db_path, min_buy_sol: float, now: float | None = None) -> dict:
    """Quality and coverage of the harvest, read-only (worker thread)."""
    now = now or time.time()
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    out = {"tokens_24h": 0, "rows": 0, "usable": 0, "bundle": 0, "relayed": 0, "dust": 0, "creator": 0,
           "sells": 0, "analyst_tokens": 0, "analyst_read": 0, "analyst_complete": 0,
           "smart": 0, "smart_funder": 0, "smart_balance": 0, "log": {}}
    try:
        try:
            r = db.execute(
                "SELECT COUNT(DISTINCT t.mint) AS tokens, COUNT(x.mint) AS rows, "
                "SUM(x.side='buy' AND x.signer=1 AND x.bundle=0 AND x.creator=0 AND x.sol>=?) AS usable, "
                "SUM(x.side='buy' AND x.bundle=1) AS bundle, SUM(x.side='buy' AND x.signer=0 AND x.bundle=0) AS relayed, "
                "SUM(x.side='buy' AND x.signer=1 AND x.bundle=0 AND x.creator=0 AND x.sol<?) AS dust, "
                "SUM(x.side='buy' AND x.creator=1) AS creator, SUM(x.side='sell') AS sells "
                "FROM sw_tokens t LEFT JOIN sw_trades x ON x.mint = t.mint "
                "WHERE t.complete=1 AND t.scanned_ts >= ?", (min_buy_sol, min_buy_sol, now - 86400)).fetchone()
            out.update({"tokens_24h": r["tokens"] or 0, "rows": r["rows"] or 0, "usable": r["usable"] or 0,
                        "bundle": r["bundle"] or 0, "relayed": r["relayed"] or 0, "dust": r["dust"] or 0,
                        "creator": r["creator"] or 0, "sells": r["sells"] or 0})
            an = {x["mint"] for x in db.execute("SELECT DISTINCT mint FROM decisions WHERE stage='analyst' AND ts >= ? "
                                                "AND mint LIKE '%pump'", (now - 6 * 3600,))}
            if an:
                read = {x["mint"]: x["complete"] for x in db.execute(
                    "SELECT mint, complete FROM sw_tokens WHERE mint IN (%s)" % ",".join("?" * len(an)), sorted(an))}
                out.update(analyst_tokens=len(an), analyst_read=len(read),
                           analyst_complete=sum(1 for v in read.values() if v))
            r = db.execute("SELECT COUNT(*) AS n, SUM(f.wallet IS NOT NULL) AS funder, "
                           "SUM(s.balance_usd IS NOT NULL) AS bal FROM sw_smart s "
                           "LEFT JOIN wallet_funders f ON f.wallet = s.wallet").fetchone()
            out.update(smart=r["n"] or 0, smart_funder=r["funder"] or 0, smart_balance=r["bal"] or 0)
            out["log"] = {f"{x['event']}:{x['reason']}": x["n"] for x in db.execute(
                "SELECT event, reason, COUNT(*) AS n FROM sw_log WHERE ts >= ? GROUP BY event, reason",
                (now - 86400,))}
        except sqlite3.OperationalError:          # Whale checker never ran
            pass
    finally:
        db.close()
    return out


def harvest_checks(eff: dict | None) -> list[dict]:
    """Health checks on the Whale checker's own counters (Controller health loop, every minute)."""
    out: list[dict] = []
    if not eff:
        return out

    def f(level, check, detail):
        return {"level": level, "agent": "Whale checker", "check": check, "detail": detail}
    if eff["scans"] >= 10 and (eff["too_long_share"] or 0) >= 0.4:
        out.append(f("warn", "molti token non leggibili",
                     f"{eff['too_long']} su {eff['scans']} token troppo lunghi da leggere nelle ultime "
                     f"{eff['window_h']:g} h ({eff['calls_wasted_too_long']} chiamate RPC spese senza risultato)"))
    if eff["errors"] >= 10:
        out.append(f("warn", "errori RPC", f"{eff['errors']} letture fallite nelle ultime {eff['window_h']:g} h"))
    if eff["backlog"] >= 100:
        out.append(f("warn", "non tiene il passo", f"{eff['backlog']} token seguiti ancora da leggere "
                                                   f"({eff['tokens_per_hour']:g} letti all'ora)"))
    if eff["reports"] >= 10 and (eff["report_timeouts"] / eff["reports"] >= 0.2 or (eff["report_secs"] or 0) > 10):
        out.append(f("warn", "rapporti lenti all'Analyst",
                     f"{eff['report_timeouts']} su {eff['reports']} senza risposta in tempo, tempo medio "
                     f"{eff['report_secs'] or 0:.1f} s"))
    if eff["entries_tried"] >= 10 and eff["entries_ok"] / eff["entries_tried"] < 0.5:
        out.append(f("info", "prezzi d'acquisto poco leggibili",
                     f"letti {eff['entries_ok']} su {eff['entries_tried']} (l'Executor usa allora la regola normale)"))
    return out


def harvest_proposals(eff: dict | None, hv: dict | None, cfg) -> list[dict]:
    """[smart_wallets] settings that would make the harvest better or cheaper. Proposals only."""
    if not eff or eff["scans"] < 20:
        return []
    c = cfg.smart_wallets
    use = eff["calls_per_min"] / eff["lane_per_min"] if eff["lane_per_min"] else 1.0
    base = {"agent": "Whale checker", "situation": "all", "kind": "whale_harvest", "verdict": "promettente",
            "gain": 0.0, "lo": 0.0, "hi": 0.0, "n": eff["scans"], "valid_n": eff["scans"], "also": []}
    out = []
    if (eff["too_long_share"] or 0) >= 0.3 and use < 0.7 and c.max_sig_pages < 150:
        out.append({**base, "label": f"{eff['too_long_share']:.0%} dei token non letti perché troppo lunghi, canale "
                                     f"RPC usato al {use:.0%}: leggere più a fondo",
                    "changes": [f"[smart_wallets] max_sig_pages = {min(150, int(c.max_sig_pages * 1.5))}"]})
    if eff["backlog"] >= 100 and use >= 0.85 and c.scan_trades > 20:
        out.append({**base, "label": f"{eff['backlog']} token in coda e canale RPC pieno: meno scambi letti per token",
                    "changes": [f"[smart_wallets] scan_trades = {max(20, int(c.scan_trades) - 10)}"]})
    usable = eff.get("usable_per_token")
    if usable is None:
        usable = (hv or {}).get("usable", 0) / max(1, (hv or {}).get("tokens_24h", 0))
    if hv and hv.get("tokens_24h", 0) >= 20 and usable < 5 and eff["backlog"] < 20 and use < 0.7 \
            and c.scan_trades < 80:
        out.append({**base, "label": f"solo {usable:.1f} compratori utili per token e canale libero: leggere più "
                                     f"scambi per token",
                    "changes": [f"[smart_wallets] scan_trades = {min(80, int(c.scan_trades) + 20)}"]})
    return out


def harvest_section(eff: dict | None, hv: dict | None, cfg) -> str:
    lines = ["", "", "## Whale checker — raccolta degli indirizzi (efficienza)"]
    if not eff:
        return "\n".join(lines + ["- Whale checker non attivo"])
    c = cfg.smart_wallets
    fmt = lambda x, d=1: "n/d" if x is None else f"{x:.{d}f}"      # noqa: E731
    use = eff["calls_per_min"] / eff["lane_per_min"] if eff["lane_per_min"] else 0.0
    lines += [f"- ultime {eff['window_h']:g} h: {eff['scans']} token letti ({eff['tokens_per_hour']:g}/ora), "
              f"{eff['complete']} completi, {eff['too_long']} troppo lunghi, {eff['errors']} errori",
              f"- costo per token letto: {fmt(eff['secs_per_token'], 0)} s, {fmt(eff['calls_per_token'], 0)} chiamate RPC, "
              f"{fmt(eff['pages_per_token'])} pagine di firme",
              f"- chiamate spese sui token troppo lunghi: {eff['calls_wasted_too_long']} su {eff['calls_total']}",
              f"- resa: {fmt(eff.get('usable_per_token'))} compratori utili per token, "
              f"{fmt(eff.get('tx_per_usable'))} transazioni lette per compratore utile, {eff.get('dust', 0)} acquisti "
              f"polvere (sotto {c.min_buy_sol:g} SOL) scartati senza salvarli",
              f"- canale RPC del Whale checker: {eff['calls_per_min']:g} chiamate/min su {eff['lane_per_min']:g} "
              f"disponibili ({use:.0%})",
              f"- coda: {eff['backlog']} token seguiti da leggere, {eff['backfill_left']} token passati da recuperare",
              f"- rapporti all'Analyst: {eff['reports']}, tempo medio {fmt(eff['report_secs'])} s, "
              f"{eff['report_timeouts']} senza risposta in tempo; prezzi d'acquisto dei wallet bravi letti "
              f"{eff['entries_ok']} su {eff['entries_tried']}"]
    if hv and hv.get("tokens_24h"):
        n = hv["tokens_24h"]
        lines += [f"- raccolto (ultime 24 h, {n} token letti): {hv['usable']} compratori utili "
                  f"({hv['usable'] / n:.1f} per token); scartati: {hv['bundle']} bundle del creatore, "
                  f"{hv['relayed']} ricevuti senza firmare, {hv['creator']} del creatore; {hv['sells']} vendite"]
    if hv and hv.get("analyst_tokens"):
        lines.append(f"- copertura: dei {hv['analyst_tokens']} token valutati dall'Analyst nelle ultime 6 h, "
                     f"{hv['analyst_read']} già letti ({hv['analyst_complete']} completi)")
    if hv and hv.get("smart"):
        lines.append(f"- wallet bravi: {hv['smart']}, finanziatore noto per {hv['smart_funder']}, saldo letto per "
                     f"{hv['smart_balance']} ({eff['smart']} sopra ${c.min_balance_usd:,.0f})")
    lg = (hv or {}).get("log") or {}
    if lg:
        names = {"chosen:smart": "entrati nella lista", "dropped:criteria": "usciti dalla lista",
                 "dropped:balance_unknown": "usciti (saldo non letto)", "discarded:hit_rate": "quasi bravi (% successi bassa)",
                 "discarded:list_size": "fuori dai primi della lista", "chosen:counted": "contati per un token",
                 "discarded:self_proof": "scartati: bravi solo grazie a quel token",
                 "discarded:insider": "scartati: insider del creatore", "discarded:selling": "scartati: stavano vendendo",
                 "discarded:same_operator": "scartati: stesso operatore di un altro",
                 "discarded:sold": "scartati: hanno venduto durante l'attesa dell'Executor"}
        lines.append("- registro delle scelte (ultime 24 h): " + "; ".join(
            f"{names.get(k, k)} {v}" for k, v in sorted(lg.items(), key=lambda kv: -kv[1])))
        disc = sum(v for k, v in lg.items() if k.startswith("discarded:") and k not in ("discarded:hit_rate",
                                                                                        "discarded:list_size"))
        chosen = lg.get("chosen:counted", 0)
        if disc + chosen >= 20 and disc > 2 * chosen:
            lines.append(f"- [INFO] per i token valutati i wallet bravi scartati ({disc}) sono più del doppio di quelli "
                         f"contati ({chosen}): guarda i motivi qui sopra")
    checks = harvest_checks(eff)
    lines += [f"- [{x['level'].upper()}] {x['check']}: {x['detail']}" for x in checks] or ["- nessun problema"]
    props = harvest_proposals(eff, hv, cfg)
    lines.append("- proposta: " + ("; ".join(p["changes"][0] for p in props) + " (vedi Proposte)" if props else
                                   "nessuna: la raccolta va bene così per ora"))
    return "\n".join(lines)
