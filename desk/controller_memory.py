"""CONTROLLER MEMORY — the Controller's own decisions: remembering why, checking them later.

BASE RULE (user, 2026-11-11 23:31 UTC): the Controller only PROPOSES, the user decides. It never applies a change
and never rolls one back by itself: auto_apply_on() is always False, [controller] auto_apply is clamped to "off",
and a disproved decision becomes a roll-back PROPOSAL in the proposals summary (rollback_proposals).
The rest of this module documents how decisions were applied before that rule (2026-11-10/11); the decisions
recorded then, and the values in data/controller_overrides_<mode>.json, stay until the user decides on them:

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
(desk/config.py), so the desk restarts with the same settings: they are loaded whatever auto_apply says, so nothing
changes silently. Only the user removes them (or moves them into config.toml).
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
    """Always False: base rule (user, 2026-11-11), the Controller never applies its decisions by itself."""
    return False


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
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg.warnings.append(f"config: {p.name} unreadable, Controller settings ignored")
        return {}
    applied = {}
    for name, v in (data or {}).items():
        sec, _, key = name.partition(".")
        if sec == "entry" and getattr(cfg.entry, "trial_since", ""):
            continue                      # the user's entry-timing trial wins over the Controller's choice
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


def toml_value(v) -> str:
    """A value written the way config.toml (and the proposals) spell it."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:g}"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(v)


def rollback_proposals(store, cfg) -> list[dict]:
    """Decisions the newer data disproved ("smentita") whose values are still in use: going back to the values
    before them is PROPOSED to the user, never done by the Controller."""
    out = []
    for d in decisions(store):
        if d["kind"] != "apply" or d["status"] != "smentita" or not d["before"] or not d["after"]:
            continue
        try:
            cur = {k: getattr(getattr(cfg, k.split(".")[0]), k.split(".")[1]) for k in d["after"]}
        except AttributeError:
            continue
        if cur != d["after"]:
            continue                                   # already changed since: nothing to roll back
        rv = d.get("review") or {}
        changes = [f"[{k.split('.')[0]}] {k.split('.')[1]} = {toml_value(v)}" for k, v in d["before"].items()]
        out.append({"agent": d["agent"], "situation": "all", "kind": "rollback", "decision": d["id"],
                    "label": f"annulla la decisione #{d['id']} del "
                             f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(d['ts']))} UTC",
                    "changes": changes, "verdict": "annullamento", "gain": -rv.get("mean", 0.0),
                    "lo": -rv.get("hi", 0.0), "hi": -rv.get("lo", 0.0), "n": rv.get("n", 0),
                    "valid_n": rv.get("n", 0), "also": []})
    return out


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
    lines = ["## Decisioni del Controllore (memoria delle scelte)",
             "Il Controllore propone soltanto: non applica e non annulla nulla da solo (regola base dell'utente, "
             "2026-11-11). Le decisioni qui sotto sono state applicate prima di questa regola e vengono ancora "
             "verificate sui token arrivati dopo; se i dati le smentiscono, l'annullamento compare tra le proposte."]
    ov = getattr(cfg, "controller_overrides", {}) or {}
    if ov:
        lines.append("Impostazioni decise in passato dal Controllore e ancora in uso (data/controller_overrides_"
                     f"{cfg.mode}.json, sopra config.toml) finché non decidi tu: "
                     + ", ".join(f"{k} = {toml_value(v)}" for k, v in sorted(ov.items())))
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
