"""MODELLO — a small statistical model trained by the Controller on the desk's own 60-minute follow-ups.

What it learns
    Every token the Analyst scores is followed for 60 minutes by the Shadow Tracker (bought or not), and the row
    keeps ~35 numbers seen at decision time (liquidity, volume, buy share, holders, the Analyst's components,
    BTC / SOL / meme index). The model learns P(win): the chance that the desk's CURRENT exit rules (hard stop,
    trailing, time stop, replayed on the recorded price path) would have closed that token in profit after costs.

How
    Gradient-boosted shallow trees (depth 2, strong shrinkage) on quantile bins, numpy only, CPU, a few seconds.
    Missing values have their own bin, so a missing site or GMGN figure neither helps nor hurts by itself.

Overfitting guard (same idea as the Controller's experiments)
    rows sorted by time ─▶ oldest 70% = training, newest 30% = verification (never seen while training)
    inside the training part: oldest 80% fits the trees, newest 20% picks how many trees and the threshold.
    On the verification part the model's block is judged on the tokens the desk would buy:
        gain per token = −return if the model blocks it, 0 otherwise   (blocking a loser = gain)
    SOLIDA       mean gain > 0 with the whole 95% interval above 0 (and enough blocked tokens)
    PROMETTENTE  mean gain > 0, interval still touching 0
    otherwise    no advantage

What it may do
    It can only BLOCK a buy that every other rule allowed, like QUANT. It never forces a trade and never loosens a
    rule. It starts "in prova" (computes and logs P(win), blocks nothing). It blocks only after a SOLIDA verdict,
    or PROMETTENTE for [controller] confirm_rounds rounds in a row, and only if the USER approved it with
    [model] enforce = true (base rule 2026-11-11: the Controller only proposes). Until then a verdict that would
    switch it on appears as a PROPOSAL (`wanted`). Once approved, a later round without advantage switches the
    block off again, as part of the rule the user approved.
    The model is retrained every Controller round; the data it learns from is never deleted.
"""
from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass

import numpy as np

# Numbers the desk saw at decision time (desk/research.py market_features / rug_features, Analyst components,
# market regime). Everything else in the stored features (dex, source, URLs) is ignored.
FEATURES = (
    "liq_usd", "vol_h1", "turnover", "txns_h1", "buy_ratio_h1", "buy_ratio_m5", "chg_m5", "chg_h1", "chg_h6",
    "age_min", "mcap", "top10_pct", "holders", "insider_pct", "smart_wallets",
    "score", "raw_score", "cap_score",
    "c_liquidity", "c_turnover", "c_buy_pressure", "c_momentum", "c_holders", "c_hygiene", "c_website",
    "c_maturity", "c_smart_money", "c_traction",
    "mkt_btc_h1", "mkt_sol_h1", "mkt_meme_h1", "mkt_btc_h24", "mkt_sol_h24", "mkt_meme_h24",
    "mkt_launch_h1", "mkt_launch_vol",
)
LABELS_IT = {
    "liq_usd": "liquidità", "vol_h1": "volume 1h", "turnover": "volume/liquidità", "txns_h1": "transazioni 1h",
    "buy_ratio_h1": "quota acquisti 1h", "buy_ratio_m5": "quota acquisti 5m", "chg_m5": "variazione 5m",
    "chg_h1": "variazione 1h", "chg_h6": "variazione 6h", "age_min": "età", "mcap": "market cap",
    "top10_pct": "top-10 holder %", "holders": "numero holder", "insider_pct": "insider %",
    "smart_wallets": "smart wallet GMGN", "score": "punteggio Analyst", "raw_score": "punteggio grezzo",
    "cap_score": "tetto punteggio", "mkt_btc_h1": "BTC 1h", "mkt_sol_h1": "SOL 1h", "mkt_meme_h1": "indice meme 1h",
    "mkt_btc_h24": "BTC 24h", "mkt_sol_h24": "SOL 24h", "mkt_meme_h24": "indice meme 24h",
    "mkt_launch_h1": "lanci nuovi 1h", "mkt_launch_vol": "volume lanci nuovi",
}
N_BINS = 16            # quantile bins per feature; bin 0 = missing
LR = 0.05              # shrinkage per tree
LAMBDA = 5.0           # L2 on leaf values
MAX_TREES = 300
PATIENCE = 30          # early stop: trees without improvement on the inner check set
BLOCK_SHARES = (0.1, 0.2, 0.3, 0.4, 0.5)   # threshold candidates: block this share of the tradeable tokens


def _num(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return math.nan
    v = float(v)
    return v if math.isfinite(v) else math.nan


def vector(f: dict) -> list[float]:
    return [_num(f.get(k)) for k in FEATURES]


# ── binning ──
def make_edges(X: np.ndarray) -> list[list[float]]:
    edges = []
    qs = np.linspace(0, 1, N_BINS + 1)[1:-1]
    for j in range(X.shape[1]):
        col = X[:, j]
        col = col[~np.isnan(col)]
        edges.append(sorted({float(x) for x in np.quantile(col, qs)}) if col.size else [])
    return edges


def binarize(X: np.ndarray, edges: list[list[float]]) -> np.ndarray:
    out = np.zeros(X.shape, dtype=np.int64)
    for j, e in enumerate(edges):
        col = X[:, j]
        ok = ~np.isnan(col)
        out[ok, j] = np.searchsorted(np.asarray(e, dtype=float), col[ok], side="right") + 1
    return out


def bin_one(x: float, e: list[float]) -> int:
    return 0 if math.isnan(x) else bisect.bisect_right(e, x) + 1


# ── trees ──
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _best_split(B: np.ndarray, idx: np.ndarray, g: np.ndarray, h: np.ndarray, min_leaf: int):
    """Best 'bin <= b goes left' split over all features for the rows in idx. Returns (gain, j, b) or None."""
    G, H = g[idx].sum(), h[idx].sum()
    parent = G * G / (H + LAMBDA)
    best = None
    for j in range(B.shape[1]):
        col = B[idx, j]
        cnt = np.bincount(col, minlength=N_BINS + 2)
        if (cnt > 0).sum() < 2:
            continue
        gs = np.cumsum(np.bincount(col, weights=g[idx], minlength=N_BINS + 2))
        hs = np.cumsum(np.bincount(col, weights=h[idx], minlength=N_BINS + 2))
        cs = np.cumsum(cnt)
        ok = (cs >= min_leaf) & (cs <= idx.size - min_leaf)
        if not ok.any():
            continue
        gain = gs * gs / (hs + LAMBDA) + (G - gs) ** 2 / (H - hs + LAMBDA) - parent
        gain = np.where(ok, gain, -np.inf)
        b = int(np.argmax(gain))
        if gain[b] > 1e-9 and (best is None or gain[b] > best[0]):
            best = (float(gain[b]), j, b)
    return best


def _grow(B, idx, g, h, depth, min_leaf, imp):
    if depth > 0 and idx.size >= 2 * min_leaf:
        sp = _best_split(B, idx, g, h, min_leaf)
        if sp is not None:
            gain, j, b = sp
            imp[j] += gain
            left = idx[B[idx, j] <= b]
            right = idx[B[idx, j] > b]
            return {"f": j, "b": b, "l": _grow(B, left, g, h, depth - 1, min_leaf, imp),
                    "r": _grow(B, right, g, h, depth - 1, min_leaf, imp)}
    return {"v": float(-LR * g[idx].sum() / (h[idx].sum() + LAMBDA))}


def _apply(tree: dict, B: np.ndarray) -> np.ndarray:
    if "v" in tree:
        return np.full(B.shape[0], tree["v"])
    go_left = B[:, tree["f"]] <= tree["b"]
    out = np.empty(B.shape[0])
    out[go_left] = _apply(tree["l"], B[go_left])
    out[~go_left] = _apply(tree["r"], B[~go_left])
    return out


def _apply_one(tree: dict, b: list[int]) -> float:
    while "v" not in tree:
        tree = tree["l"] if b[tree["f"]] <= tree["b"] else tree["r"]
    return tree["v"]


def fit(B: np.ndarray, y: np.ndarray, n_trees: int, min_leaf: int, depth: int = 2,
        B_check: np.ndarray | None = None, y_check: np.ndarray | None = None, seed: int = 7):
    """Boosted trees on binned data. With a check set: stops when its log-loss stops improving and keeps the best
    number of trees. Returns (base, trees, importance)."""
    rng = np.random.default_rng(seed)
    p0 = min(max(float(y.mean()), 1e-3), 1 - 1e-3)
    base = math.log(p0 / (1 - p0))
    F = np.full(y.size, base)
    Fc = np.full(y_check.size, base) if y_check is not None else None
    trees, imp = [], np.zeros(B.shape[1])
    best_loss, best_n = math.inf, 0
    for t in range(n_trees):
        p = _sigmoid(F)
        g, h = p - y, np.maximum(p * (1 - p), 1e-6)
        idx = np.sort(rng.choice(y.size, size=max(2 * min_leaf, int(y.size * 0.8)), replace=False))
        tree = _grow(B, idx, g, h, depth, min_leaf, imp)
        trees.append(tree)
        F += _apply(tree, B)
        if Fc is not None:
            Fc += _apply(tree, B_check)
            pc = np.clip(_sigmoid(Fc), 1e-6, 1 - 1e-6)
            loss = float(-np.mean(y_check * np.log(pc) + (1 - y_check) * np.log(1 - pc)))
            if loss < best_loss - 1e-6:
                best_loss, best_n = loss, t + 1
            elif t + 1 - best_n >= PATIENCE:
                break
    if Fc is not None:
        trees = trees[:max(best_n, 1)]
    return base, trees, imp


def predict(base: float, trees: list[dict], B: np.ndarray) -> np.ndarray:
    F = np.full(B.shape[0], base)
    for t in trees:
        F += _apply(t, B)
    return _sigmoid(F)


def auc(p: np.ndarray, y: np.ndarray) -> float | None:
    pos, neg = int(y.sum()), int(y.size - y.sum())
    if pos == 0 or neg == 0:
        return None
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(p.size)
    ranks[order] = np.arange(1, p.size + 1)
    for v in np.unique(p):                          # average ranks on ties
        m = p == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def _gain_stats(p: np.ndarray, ret: np.ndarray, thr: float, z: float = 1.96) -> dict:
    d = np.where(p < thr, -ret, 0.0)
    n = d.size
    m = float(d.mean()) if n else 0.0
    sd = float(d.std(ddof=1)) if n > 1 else 0.0
    h = z * sd / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": m, "lo": m - h, "hi": m + h, "blocked": int((p < thr).sum()),
            "blocked_avg_ret": float(ret[p < thr].mean()) if (p < thr).any() else 0.0,
            "kept_avg_ret": float(ret[p >= thr].mean()) if (p >= thr).any() else 0.0}


# ── one training round (pure CPU, no I/O: runs in a worker thread) ──
def dataset(analyst_raw: list[dict], cfg) -> dict:
    """Rows -> features, label and the return of the CURRENT exit rules (replayed when a price path exists)."""
    from .controller import parse_rows
    rows = parse_rows(analyst_raw, cfg.analyst.young_age_hours * 60, cfg)   # untrustworthy paths left out
    X, ret, trade, ts = [], [], [], []
    for x in rows:
        rr = x["ret"]          # current exit rules replayed on the path (parse_rows), else the live simulation
        X.append(vector(x["f"]))
        ret.append(rr)
        s = x["f"].get("score")
        trade.append(isinstance(s, (int, float)) and s >= cfg.analyst.min_score)
        ts.append(x["ts"])
    X = np.asarray(X, dtype=float).reshape(len(rows), len(FEATURES))
    ret = np.asarray(ret, dtype=float)
    return {"X": X, "ret": ret, "y": (ret > 0).astype(float), "trade": np.asarray(trade, dtype=bool),
            "ts": ts, "with_path": sum(1 for x in rows if x["path"]), "mints": len({x["mint"] for x in rows})}


def train_round(analyst_raw: list[dict], cfg) -> dict:
    m = cfg.model
    holdout = cfg.controller.holdout_pct / 100
    ds = dataset(analyst_raw, cfg)
    X, y, ret, trade = ds["X"], ds["y"], ds["ret"], ds["trade"]
    n = y.size
    out = {"t": time.time(), "rows": n, "tradeable": int(trade.sum()), "with_path": ds["with_path"],
           "tokens": ds["mints"], "win_rate": float(y.mean()) if n else 0.0,
           "since": ds["ts"][0] if n else None, "min_rows": m.min_rows, "verdict": "dati insufficienti"}
    if n < m.min_rows:
        return out
    cut = int(n * (1 - holdout))
    icut = int(cut * 0.8)
    if min(icut, cut - icut, n - cut) < 2 * m.min_leaf or len(set(y[:icut])) < 2:
        return out
    # 1) trees + threshold chosen on the training part only
    edges = make_edges(X[:cut])
    B = binarize(X, edges)
    base, trees, _ = fit(B[:icut], y[:icut], MAX_TREES, m.min_leaf, B_check=B[icut:cut], y_check=y[icut:cut])
    n_trees = len(trees)
    p_in = predict(base, trees, B[icut:cut])
    sel = trade[icut:cut] if trade[icut:cut].sum() >= 20 else np.ones(cut - icut, dtype=bool)
    thr, best = None, 0.0
    for share in (s for s in BLOCK_SHARES if s <= m.max_block_pct / 100 + 1e-9):
        t = float(np.quantile(p_in[sel], share))
        st = _gain_stats(p_in[sel], ret[icut:cut][sel], t)
        if st["blocked"] >= 3 and st["mean"] > best:
            thr, best = t, st["mean"]
    # 2) refit on the whole training part with that many trees, judge on the newest 30% it never saw
    base, trees, _ = fit(B[:cut], y[:cut], n_trees, m.min_leaf)
    p_va = predict(base, trees, B[cut:])
    out["auc_valid"] = auc(p_va, y[cut:])
    out["auc_train"] = auc(predict(base, trees, B[:cut]), y[:cut])
    out["n_trees"] = n_trees
    vsel = trade[cut:] if trade[cut:].sum() >= m.min_eval_rows else np.ones(n - cut, dtype=bool)
    out["valid_universe"] = "token sopra soglia" if trade[cut:].sum() >= m.min_eval_rows \
        else "tutti i token valutati (quelli sopra soglia sono ancora pochi)"
    if thr is None:
        out["verdict"] = "nessun vantaggio"
        out["why"] = "sui dati di addestramento nessuna soglia avrebbe evitato più perdite che guadagni"
    else:
        va = _gain_stats(p_va[vsel], ret[cut:][vsel], thr)
        out["valid"] = va
        if va["mean"] > 0 and va["lo"] > 0 and va["blocked"] >= 5 and va["n"] >= m.min_eval_rows:
            out["verdict"] = "solida"
        elif va["mean"] > 0:
            out["verdict"] = "promettente"
        else:
            out["verdict"] = "nessun vantaggio"
    # 3) the model used live: same recipe on ALL rows (newest data included), same number of trees and threshold
    edges = make_edges(X)
    B = binarize(X, edges)
    base, trees, imp = fit(B, y, n_trees, m.min_leaf)
    tot = imp.sum() or 1.0
    out["importance"] = [(FEATURES[j], float(imp[j] / tot)) for j in np.argsort(-imp)[:8] if imp[j] > 0]
    out["model"] = {"base": base, "trees": trees, "edges": edges, "threshold": thr, "features": list(FEATURES)}
    return out


# ── the live gate (owned by the desk, updated by the Controller) ──
@dataclass
class ModelVerdict:
    p: float
    threshold: float | None
    active: bool
    block: bool

    def short(self) -> str:
        t = f" < soglia {self.threshold:.2f}" if self.block else \
            (f" (soglia {self.threshold:.2f}{'' if self.active else ', in prova'})" if self.threshold is not None else
             " (in prova)")
        return f"MODEL P(win) {self.p:.2f}{t}"

    @property
    def reason(self) -> str:
        return (f"MODEL: P(win) {self.p:.2f} < {self.threshold:.2f} (modello statistico addestrato dal Controllore "
                "sui follow-up a 60 minuti, verificato su dati mai visti)")


class ModelGate:
    """Holds the latest trained model. Survives restarts (kv 'ml_model')."""

    KV = "ml_model"

    def __init__(self, store, cfg) -> None:
        self.store, self.cfg = store, cfg
        st = store.kv_get(self.KV, {}) or {}
        self.model: dict | None = st.get("model")
        self.last: dict = st.get("last", {})
        self.active: bool = bool(st.get("active", False)) and bool(cfg.model.enforce)   # only if the user approved
        self.streak: int = int(st.get("streak", 0))
        self.history: list = st.get("history", [])
        self.calls = 0
        self.blocks = 0
        self.wanted = False

    def _save(self) -> None:
        self.store.kv_set(self.KV, {"model": self.model, "last": self.last, "active": self.active,
                                    "streak": self.streak, "history": self.history[-50:]})

    def update(self, res: dict, may_apply: bool) -> str | None:
        """Takes a training round's result. Returns a message when the block switched on or off."""
        was = self.active
        if res.get("model") is not None:
            self.model = res["model"]
        v = res.get("verdict")
        self.streak = self.streak + 1 if v == "promettente" else 0
        if v == "solida":
            on = True
        elif v == "promettente":      # needs confirm_rounds in a row to switch on; an active block stays on
            on = was or self.streak >= self.cfg.controller.confirm_rounds
        else:                         # no advantage, or too little data in the lookback window: block off
            on = False
        ready = bool(on and self.cfg.model.enabled and self.model is not None
                     and self.model.get("threshold") is not None)
        self.wanted = ready                      # what the verdict asks for: a proposal until the user approves it
        self.active = bool(ready and may_apply and self.cfg.model.enforce)
        self.last = {k: v for k, v in res.items() if k != "model"}
        msg = None
        if self.active != was:
            va = res.get("valid") or {}
            msg = (("Modello ATTIVO: blocca gli acquisti con P(win) sotto "
                    f"{self.model['threshold']:.2f}. Sui {va.get('n', 0)} token più recenti mai visti avrebbe "
                    f"bloccato {va.get('blocked', 0)} token (rendimento medio {va.get('blocked_avg_ret', 0):+.1f}%) "
                    f"guadagnando {va.get('mean', 0):+.2f} punti % per token [{va.get('lo', 0):+.2f}, "
                    f"{va.get('hi', 0):+.2f}].") if self.active else
                   f"Modello in prova: il blocco è spento perché sui dati più recenti il verdetto è '{v}'.")
            self.history.append({"ts": time.time(), "active": self.active, "verdict": v, "msg": msg})
        self._save()
        return msg

    def evaluate(self, features: dict) -> ModelVerdict | None:
        m = self.model
        if m is None or not self.cfg.model.enabled:
            return None
        x = [_num(features.get(k)) for k in m["features"]]
        b = [bin_one(v, e) for v, e in zip(x, m["edges"])]
        F = m["base"] + sum(_apply_one(t, b) for t in m["trees"])
        p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, F))))
        thr = m.get("threshold")
        block = bool(self.active and thr is not None and p < thr)
        self.calls += 1
        self.blocks += block
        return ModelVerdict(p, thr, self.active, block)

    def status(self) -> str:
        if not self.cfg.model.enabled:
            return "spento"
        if self.model is None:
            return "in attesa di dati"
        return "attivo" if self.active else "in prova"

    def summary(self) -> dict:
        last = self.last or {}
        return {"status": self.status(), "verdict": last.get("verdict"), "rows": last.get("rows"),
                "min_rows": last.get("min_rows", self.cfg.model.min_rows), "auc": last.get("auc_valid"),
                "threshold": (self.model or {}).get("threshold"), "valid": last.get("valid"),
                "streak": self.streak, "blocks": self.blocks, "calls": self.calls, "ts": last.get("t"),
                "wanted": self.wanted}


# ── report ──
def _fmt_auc(a) -> str:
    return "n/d" if a is None else f"{a:.3f}"


def report_section(res: dict | None, gate: ModelGate | None, cfg) -> str:
    L = ["", "## Modello statistico (P(win) sui follow-up a 60 minuti)", ""]
    if not cfg.model.enabled:
        return "\n".join(L + ["Spento ([model] enabled = false).", ""])
    if not res:
        return "\n".join(L + ["Primo addestramento al prossimo ciclo del Controllore.", ""])
    L.append(f"- Dati: {res['rows']} valutazioni dell'Analyst su {res.get('tokens', 0)} token "
             f"({res.get('with_path', 0)} con percorso di prezzo, {res.get('tradeable', 0)} sopra la soglia "
             f"attuale), vincenti con le regole di uscita attuali: {res.get('win_rate', 0) * 100:.1f}%.")
    if res["verdict"] == "errore":
        return "\n".join(L + [f"- {res.get('why')}. Il blocco resta spento.", ""])
    if res["verdict"] == "dati insufficienti":
        L.append(f"- In attesa di dati: servono almeno {res['min_rows']} valutazioni con i valori salvati "
                 f"(ora {res['rows']}). Fino ad allora il modello non esiste e non blocca nulla.")
        return "\n".join(L + [""])
    L.append(f"- Alberi: {res.get('n_trees')}; capacità di distinguere vincenti e perdenti (AUC, 0.5 = caso): "
             f"addestramento {_fmt_auc(res.get('auc_train'))}, dati nuovi mai visti {_fmt_auc(res.get('auc_valid'))}.")
    va = res.get("valid")
    if va:
        L.append(f"- Verifica sul {cfg.controller.holdout_pct:g}% più recente ({res.get('valid_universe')}, {va['n']} "
                 f"token): bloccando quelli con P(win) < {res['model']['threshold']:.2f} avrebbe evitato "
                 f"{va['blocked']} token (rendimento medio {va['blocked_avg_ret']:+.2f}%) e tenuto gli altri "
                 f"(medio {va['kept_avg_ret']:+.2f}%): guadagno {va['mean']:+.2f} punti % per token, intervallo 95% "
                 f"[{va['lo']:+.2f}, {va['hi']:+.2f}].")
    elif res.get("why"):
        L.append(f"- {res['why']}.")
    L.append(f"- Verdetto: **{res['verdict'].upper()}**.")
    if gate is not None:
        st = gate.status()
        L.append(f"- Stato del bot: **{st}**" + (
            " (blocca gli acquisti sotto soglia)" if st == "attivo" else
            f" (calcola e registra P(win) senza bloccare; si attiva con un verdetto SOLIDA o PROMETTENTE per "
            f"{cfg.controller.confirm_rounds} cicli di fila, ora {gate.streak})" if st == "in prova" else
            " (il primo modello nasce al prossimo ciclo del Controllore con dati sufficienti)"
            if st == "in attesa di dati" else "") + ".")
    if res.get("importance"):
        L.append("- Valori che pesano di più: " + ", ".join(
            f"{LABELS_IT.get(k, k.replace('c_', 'punteggio '))} {w * 100:.0f}%" for k, w in res["importance"]) + ".")
    L.append("- Il modello può solo bloccare un acquisto che le altre regole permettono; non allenta mai le regole fisse.")
    return "\n".join(L + [""])


def describe_data(store, cfg) -> str:
    """`python run.py model`: how much learning data there is, then one training round."""
    since = time.time() - cfg.controller.lookback_days * 86400
    q = store.q
    L = ["# Dati di apprendimento", ""]
    tot = q("SELECT stage, COUNT(*) n, SUM(features IS NOT NULL) f, SUM(path IS NOT NULL) p, MIN(start_ts) t0, "
            "MAX(start_ts) t1 FROM shadow GROUP BY stage")
    for r in tot:
        span = ((r["t1"] or 0) - (r["t0"] or 0)) / 86400
        L.append(f"- shadow '{r['stage']}': {r['n']} follow-up ({r['f'] or 0} con i valori salvati, "
                 f"{r['p'] or 0} con percorso di prezzo), su {span:.1f} giorni")
    for t in ("trades", "trades_archive", "outcomes", "decisions", "controller_decisions", "rugged_names"):
        try:
            L.append(f"- {t}: {q(f'SELECT COUNT(*) n FROM {t}')[0]['n']}")
        except Exception:  # noqa: BLE001 - older databases may miss a table
            pass
    an = q("SELECT start_ts,mint,score,decision,sim_net_pct,cost_pct,features,path FROM shadow WHERE stage='analyst' "
           "AND features IS NOT NULL AND start_ts>=? ORDER BY start_ts DESC LIMIT 6000", (since,))
    t0 = time.time()
    res = train_round(an, cfg)
    L.append(f"\nAddestramento di prova sugli ultimi {cfg.controller.lookback_days:g} giorni: {time.time() - t0:.1f} s.")
    gate = ModelGate(store, cfg)
    return "\n".join(L) + "\n" + report_section(res, gate, cfg)
