"""CONTROLLER MEMORY — the Controller's own decisions: applying improvements, remembering why, checking them later.

When [controller] auto_apply allows it (default "paper": the paper desk only), an improvement found by the tests is
applied to the running desk by the Controller itself:

  * only changes to the general configuration ("tutte le situazioni"), only plain config keys, always inside the
    hard limits (the same check as config.toml);
  * a SOLIDA result is applied at once; a PROMETTENTE one only after it has won confirm_rounds rounds in a row;
  * at most one change per agent every apply_cooldown_minutes, so the effect of each change can be measured;
  * never a change the data already proved wrong (a "smentita" decision), never all Scout sources off, and a
    discovery source is switched off only on a SOLIDA result (losing a source also loses its future data).

Every decision is a row in the controller_decisions table, kept forever (it is learning data): what changed,
the values before and after, the numbers that led to it (tokens, window, rounds won, interval, other situations
where it won) and the reasoning in words. Then the Controller keeps checking it: on the tokens that arrived
after the change it compares the new setting with the old one (paired, same tokens). Once review_min_rows new
tokens exist the decision is marked "confermata" or "smentita"; a disproved change is rolled back automatically,
and that roll-back is recorded as its own decision.

The applied values live in data/controller_overrides_<mode>.json and are loaded over config.toml at every start
(desk/config.py), so the desk restarts with what the Controller learned. auto_apply = "off" ignores the file.
"""
from __future__ import annotations

import json
import time
import tomllib
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS controller_decisions (
  id INTEGER PRIMARY KEY, ts REAL, mode TEXT, agent TEXT, kind TEXT, changes TEXT, before TEXT, after TEXT,
  verdict TEXT, gain REAL, lo REAL, hi REAL, valid_n INTEGER, n INTEGER, streak INTEGER, also TEXT, data TEXT,
  reasoning TEXT, variant_before TEXT, variant_after TEXT, status TEXT, review TEXT, review_ts REAL);
"""
SCOUT_SOURCES = ("pumpportal", "geckoterminal", "dexscreener")


def overrides_path(data_dir: Path, mode: str) -> Path:
    return Path(data_dir) / f"controller_overrides_{mode}.json"


def auto_apply_on(cfg) -> bool:
    mode = cfg.controller.auto_apply
    return mode == "always" or (mode == "paper" and cfg.mode == "paper")


def parse_change(ch: str) -> tuple[str, str, object] | None:
    """'[risk] hard_stop_pct = -4' -> ('risk', 'hard_stop_pct', -4). None for anything that isn't a config key."""
    if not ch.startswith("[") or "] " not in ch or " = " not in ch:
        return None
    sec, rest = ch[1:].split("] ", 1)
    key, val = rest.split(" = ", 1)
    try:
        v = tomllib.loads(f"v = {val}")["v"]
    except tomllib.TOMLDecodeError:
        return None
    return sec.strip(), key.strip(), v


def _cast(cur, v):
    if isinstance(cur, bool):
        return bool(v)
    if isinstance(cur, int) and not isinstance(cur, bool):
        return int(v)
    if isinstance(cur, float):
        return float(v)
    return v


def set_values(cfg, triples: list[tuple[str, str, object]]) -> tuple[dict, dict]:
    """Apply (section, key, value) to the live config objects. Returns (before, after) as {'sec.key': value}."""
    before, after = {}, {}
    for sec, key, v in triples:
        obj = getattr(cfg, sec)
        cur = getattr(obj, key)
        nv = _cast(cur, v)
        before[f"{sec}.{key}"] = cur
        setattr(obj, key, nv)
        after[f"{sec}.{key}"] = nv
    return before, after


def load_overrides(cfg) -> dict:
    """Called by load_config: the Controller's applied values over config.toml (if auto-apply is on)."""
    p = overrides_path(cfg.data_dir, cfg.mode)
    if not auto_apply_on(cfg) or not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg.warnings.append(f"config: {p.name} unreadable, Controller settings ignored")
        return {}
    applied = {}
    for name, v in (data or {}).items():
        sec, _, key = name.partition(".")
        obj = getattr(cfg, sec, None)
        if obj is None or not hasattr(obj, key):
            continue
        try:
            setattr(obj, key, _cast(getattr(obj, key), v))
            applied[name] = v
        except (TypeError, ValueError):
            continue
    return applied


def save_overrides(cfg, after: dict) -> None:
    p = overrides_path(cfg.data_dir, cfg.mode)
    try:
        cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        cur = {}
    cur.update(after)
    p.write_text(json.dumps(cur, indent=2, sort_keys=True), encoding="utf-8")
    cfg.controller_overrides = dict(cur)


def scout_all_off(cfg, triples) -> bool:
    flags = {k: getattr(cfg.scout, k) for k in SCOUT_SOURCES}
    for sec, key, v in triples:
        if sec == "scout" and key in flags:
            flags[key] = bool(v)
    return not any(flags.values())


def choose(props: list[dict], cfg, streaks: dict, last_applied: dict, refuted: set, now: float) -> tuple[list, dict]:
    """Which proposals to apply this round (pure). Returns ([(proposal, triples, streak)], new_streaks)."""
    from .controller import within_hard_limits
    c = cfg.controller
    new_streaks: dict[str, int] = {}
    picked, agents_done = [], set()
    for p in props:
        if p["situation"] != "all":
            continue
        triples = [parse_change(ch) for ch in p["changes"]]
        if not triples or any(t is None for t in triples):
            continue                                        # not a plain config change (e.g. an Executor hint)
        key = p["agent"] + "|" + json.dumps(p["changes"])
        streak = streaks.get(key, 0) + 1
        new_streaks[key] = streak
        if key in refuted or p["agent"] in agents_done:
            continue
        if now - last_applied.get(p["agent"], 0) < c.apply_cooldown_minutes * 60:
            continue
        if not within_hard_limits(cfg, p["changes"]) or scout_all_off(cfg, triples):
            continue
        turns_off_source = p["agent"] == "Scout" and any(v is False for _, _, v in triples)
        if p["verdict"] == "solida" or (streak >= c.confirm_rounds and not turns_off_source):
            picked.append((p, triples, streak))
            agents_done.add(p["agent"])
    return picked, new_streaks


def reasoning_text(p: dict, streak: int, exp: dict, before: dict, after: dict) -> str:
    ch = ", ".join(f"{k} {before[k]!r} -> {after[k]!r}" for k in after)
    why = (f"risultato SOLIDO in verifica" if p["verdict"] == "solida"
           else f"PROMETTENTE per {streak} cicli di test consecutivi")
    also = f"; vinceva anche in: {', '.join(p['also'])}" if p.get("also") else ""
    return (f"{p['agent']}: {ch}. Scelta perché {why}: sui {p['valid_n']} token più recenti mai usati per sceglierla "
            f"ha fatto in media {p['gain']:+.2f} punti % per token rispetto alla configurazione di prima "
            f"(intervallo [{p['lo']:+.2f}, {p['hi']:+.2f}]), su {p['n']} token valutati in totale e "
            f"{exp.get('tried', 0)} configurazioni provate in questo ciclo{also}.")


def record(store, cfg, kind: str, agent: str, changes: list[str], before: dict, after: dict, p: dict | None,
           streak: int, exp: dict, reasoning: str, variant_before=None, variant_after=None) -> int:
    p = p or {}
    cur = store.db.execute(
        "INSERT INTO controller_decisions(ts,mode,agent,kind,changes,before,after,verdict,gain,lo,hi,valid_n,n,streak,"
        "also,data,reasoning,variant_before,variant_after,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), cfg.mode, agent, kind, json.dumps(changes), json.dumps(before, default=str),
         json.dumps(after, default=str), p.get("verdict", ""), p.get("gain"), p.get("lo"), p.get("hi"),
         p.get("valid_n"), p.get("n"), streak, json.dumps(p.get("also", [])),
         json.dumps({k: exp.get(k) for k in ("analyst_rows", "with_path", "filter_rows", "since", "tried")},
                    default=str),
         reasoning, json.dumps(variant_before, default=str), json.dumps(variant_after, default=str), "attiva"))
    return cur.lastrowid


def decisions(store, limit: int = 0) -> list[dict]:
    rows = store.q("SELECT * FROM controller_decisions ORDER BY ts DESC" + (f" LIMIT {int(limit)}" if limit else ""))
    for r in rows:
        for k in ("changes", "before", "after", "also", "data", "review", "variant_before", "variant_after"):
            try:
                r[k] = json.loads(r[k]) if r.get(k) else None
            except ValueError:
                r[k] = None
    return rows


def pending_reviews(store) -> list[dict]:
    """Applied decisions still being verified on new data (input for run_experiments)."""
    out = []
    for d in decisions(store):
        if d["kind"] == "apply" and d["status"] == "attiva" and d["variant_before"] and d["variant_after"]:
            out.append({"id": d["id"], "agent": d["agent"], "since": d["ts"], "before": d["variant_before"],
                        "after": d["variant_after"]})
    return out


def judge_review(rv: dict, min_rows: int) -> str | None:
    """'confermata' / 'smentita' once enough new tokens exist, else None (still being verified)."""
    if rv["n"] < min_rows:
        return None
    if rv["hi"] < 0:
        return "smentita"
    return "confermata" if rv["mean"] >= 0 else "smentita" if rv["n"] >= 3 * min_rows else None


def refuted_keys(store) -> set[str]:
    return {d["agent"] + "|" + json.dumps(d["changes"]) for d in decisions(store)
            if d["kind"] == "apply" and d["status"] == "smentita"}


def report_section(store, cfg, limit: int = 12) -> list[str]:
    ds = decisions(store, limit)
    mode = cfg.controller.auto_apply
    head = {"off": "disattivata: il Controllore propone soltanto",
            "paper": "attiva sul conto paper (in live il Controllore propone soltanto)",
            "always": "attiva anche in live"}.get(mode, mode)
    lines = ["## Decisioni del Controllore (memoria delle scelte)",
             f"Applicazione automatica {head}. Una proposta SOLIDA viene applicata subito, una PROMETTENTE dopo "
             f"{cfg.controller.confirm_rounds} cicli vinti di fila; al massimo una modifica per agente ogni "
             f"{cfg.controller.apply_cooldown_minutes:g} min; ogni scelta viene poi verificata sui token arrivati dopo."]
    ov = getattr(cfg, "controller_overrides", {}) or {}
    if ov:
        lines.append("Impostazioni ora decise dal Controllore (sopra config.toml): "
                     + ", ".join(f"{k} = {v}" for k, v in sorted(ov.items())))
    if not ds:
        lines += ["- nessuna decisione ancora", ""]
        return lines
    for d in ds:
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(d["ts"]))
        st = d["status"]
        rv = d.get("review") or {}
        check = (f" | verifica su {rv.get('n', 0)} token nuovi: {rv.get('mean', 0):+.2f} "
                 f"[{rv.get('lo', 0):+.2f}, {rv.get('hi', 0):+.2f}]") if rv else ""
        lines.append(f"- {when} UTC [{d['kind']} — {st}] {d['reasoning']}{check}")
    return lines + [""]
