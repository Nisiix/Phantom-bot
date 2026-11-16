"""REAL HORIZON — how a token really ends, not where it stands after 60 minutes (user 2026-11-16).

With no stop at a loss ([risk] loss_stop = false) a position is held until the profit stop sells it, which can take
hours, or never. The 60-minute follow-up (desk/shadow.py) valued a token that had not yet reached the profit stop at
its minute-60 price (about -22% on average), while the real buys that never reached +30% ended between -78% and -100%,
hours later. The Controller therefore rated the current settings about +6% per token while real trades made about -8%.

Every token the Analyst scores, and every token still tradeable at the end of the graduation wait, is followed here
for [realwatch] hours (24) after its 60-minute follow-up, with one DexScreener reading every few minutes appended to
its price path. Its outcome answers one question:

    up      = reached the profit stop's arming level (+30% net) before falling to down_pct (-50% net)
    down    = fell to down_pct first
    neither = neither within the window

and its result is the current exits of Risk replayed on the whole path (sold by the profit stop, else the value at the
end). A pool pulled (liquidity under dead_liquidity_ratio of the first reading) counts as -100% from then on.

Who uses it: the Controller tests on these rows once it has [realwatch] min_rows of them (desk/agents/controller.py),
QUANT learns from them (desk/quant.py learning_rows), the model trains on them (same rows as the Controller).
Measurement only: nothing is traded, and the rows are learning data (never deleted by a reset)."""
from __future__ import annotations

import asyncio
import json
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS realwatch (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT, stage TEXT, decision TEXT, score REAL,
  traded INTEGER DEFAULT 0, start_ts REAL, ref_price REAL, ref_liq REAL, cost_pct REAL, features TEXT, path TEXT,
  last_ts REAL, misses INTEGER DEFAULT 0, done INTEGER DEFAULT 0, label TEXT, ret_net REAL, max_net REAL,
  end_ts REAL, end_reason TEXT, UNIQUE(mint, stage, start_ts));
CREATE INDEX IF NOT EXISTS ix_realwatch_done ON realwatch(done);
CREATE INDEX IF NOT EXISTS ix_realwatch_mint ON realwatch(mint);
"""

STAGES = {("analyst", None), ("grad45", "buy_at_wait_end")}   # (stage, decision): None = any decision
MAX_MISSES = 3          # a token missing from a batch that answered for others this many times: no market left


def wanted(stage: str, decision: str) -> bool:
    return (stage, None) in STAGES or (stage, decision) in STAGES


def outcome(path: list, cost: float, up: float, down: float) -> str:
    """up / down / neither: which line the NET return touched first on the whole path."""
    for _, raw in path:
        net = raw - cost
        if net >= up:
            return "up"
        if net <= down:
            return "down"
    return "neither"


def replay(path: list, cost: float, cfg) -> tuple[float, float | None]:
    """(net % with the current Risk exits, second at which they sold or None = still held at the end)."""
    from .controller import simulate_exit
    from .exits import lock_rule
    r = cfg.risk
    info: dict = {}
    net = simulate_exit(path, cost, r.exit_stop_pct or r.hard_stop_pct, r.trail_activate_pct, r.trail_distance_pct,
                        float(r.max_hold_minutes or 0), lock_rule(r), info)
    return net, info.get("sold_s")


def judge(path: list, cost: float, cfg) -> dict:
    """label, result (ret_net) and best net of a followed path under the current rules."""
    from .quant import barriers
    up, _ = barriers(cfg.risk)
    label = outcome(path, cost, up, cfg.realwatch.down_pct)
    net, sold_s = replay(path, cost, cfg)
    return {"label": label, "ret_net": net, "sold_s": sold_s,
            "max_net": max((v for _, v in path), default=0.0) - cost}


class RealWatch:
    def __init__(self, desk) -> None:
        self.desk = desk
        self.cfg = desk.cfg
        self.c = desk.cfg.realwatch
        self.store = desk.store
        self.store.db.executescript(SCHEMA)
        self.closed = 0
        self.polls = 0

    # ── called by the shadow tracker when a 60-minute follow-up ends ──
    def start(self, row: dict, path: list | None) -> bool:
        """Keep following a token whose 60-minute follow-up just ended. row: the stored shadow row."""
        if not self.c.enabled or not wanted(row.get("stage", ""), row.get("decision", "")):
            return False
        if not row.get("ref_price") or row["ref_price"] <= 0:
            return False
        if self.active_count() >= self.c.max_active:
            return False
        try:
            self.store.db.execute(
                "INSERT OR IGNORE INTO realwatch(mint,symbol,stage,decision,score,traded,start_ts,ref_price,ref_liq,"
                "cost_pct,features,path,last_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["mint"], row.get("symbol"), row["stage"], row.get("decision"), row.get("score"),
                 int(row.get("traded") or 0), row["start_ts"], row["ref_price"], row.get("ref_liq") or 0.0,
                 row.get("cost_pct") or 2.5, row.get("features"),
                 json.dumps([list(p) for p in (path or [])], separators=(",", ":")),
                 row["start_ts"] + path[-1][0] if path else time.time()))   # time of the last reading
        except Exception:  # noqa: BLE001 - measurement only: never stops the shadow tracker
            return False
        return True

    def mark_traded(self, mint: str) -> None:
        self.store.db.execute("UPDATE realwatch SET traded=1 WHERE mint=? AND stage='analyst' AND done=0", (mint,))

    def active_count(self) -> int:
        try:
            return int(self.store.q("SELECT COUNT(*) AS n FROM realwatch WHERE done=0")[0]["n"])
        except Exception:  # noqa: BLE001
            return 0

    def due(self, now: float | None = None) -> list[dict]:
        t = now or time.time()
        out = []
        for r in self.store.q("SELECT id,mint,start_ts,last_ts FROM realwatch WHERE done=0"):
            age = t - r["start_ts"]
            gap = self.c.fast_poll_seconds if age < self.c.fast_minutes * 60 else self.c.poll_seconds
            if t - (r["last_ts"] or 0) >= gap - 5:
                out.append(dict(r))
        return out

    # ── one round of readings ──
    def update(self, snaps: dict, rows: list[dict], now: float | None = None) -> int:
        """Append one reading to each due row; close the rows that ended. Returns how many were closed."""
        t = now or time.time()
        answered = bool(snaps)
        closed = 0
        for d in rows:
            r = self.store.q("SELECT * FROM realwatch WHERE id=?", (d["id"],))
            if not r:
                continue
            r = dict(r[0])
            try:
                path = [tuple(p) for p in json.loads(r["path"] or "[]")]
            except (TypeError, ValueError):
                path = []
            sec = int(t - r["start_ts"])
            s = snaps.get(r["mint"])
            misses = r["misses"] or 0
            end = ""
            if s is not None and (s.price_usd or 0) > 0:
                misses = 0
                raw = (s.price_usd / r["ref_price"] - 1) * 100
                if r["ref_liq"] and s.liquidity_usd < self.c.dead_liquidity_ratio * r["ref_liq"]:
                    raw, end = -100.0, "pool svuotata"
                path.append((sec, round(raw, 2)))
            elif answered:
                misses += 1
                if misses >= MAX_MISSES:
                    path.append((sec, -100.0))
                    end = "nessun mercato"
            cost = r["cost_pct"] or 2.5
            res = judge(path, cost, self.cfg) if path else None
            if not end and res and res["sold_s"] is not None and res["label"] != "neither":
                end = "deciso"           # the profit stop sold and the outcome is known: nothing left to learn
            if not end and t - r["start_ts"] >= self.c.hours * 3600:
                end = "fine finestra"
            sets = {"path": json.dumps([list(p) for p in path], separators=(",", ":")), "last_ts": t,
                    "misses": misses}
            if end:
                res = res or {"label": "neither", "ret_net": -cost, "max_net": -cost}
                sets.update(done=1, label=res["label"], ret_net=res["ret_net"], max_net=res["max_net"], end_ts=t,
                            end_reason=end)
                closed += 1
                self._learn({**r, "path": sets["path"]}, res, t)
            cols = ", ".join(f"{k}=?" for k in sets)
            self.store.db.execute(f"UPDATE realwatch SET {cols} WHERE id=?", (*sets.values(), r["id"]))
        self.closed += closed
        return closed

    def _learn(self, r: dict, res: dict, t: float) -> None:
        """A closed Analyst row the desk did not buy becomes a QUANT sample (real trades are learned at the sale)."""
        if r["stage"] != "analyst" or r["score"] is None or r["traded"]:
            return
        q = getattr(self.desk, "quant", None)
        if q is None:
            return
        o = row_outcome({**r, **res, "end_ts": t}, self.cfg)
        if o is not None:
            q.add(o)

    async def loop(self) -> None:
        if not self.c.enabled:
            return
        while not self.desk.stop.is_set():
            try:
                rows = self.due()
                if rows:
                    snaps = await self.desk.dex.snapshots(list({r["mint"] for r in rows}), fresh=True,
                                                          lane="longwatch")
                    self.update(snaps or {}, rows)
                    self.polls += 1
            except Exception:  # noqa: BLE001 - a missed reading is caught up by the next one
                pass
            try:
                await asyncio.wait_for(self.desk.stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass


# ═══════════════════════════ readers (QUANT, Controller: read-only) ═══════════════════════════
def row_outcome(r: dict, cfg) -> dict | None:
    """A closed realwatch row -> QUANT sample (label / result by the CURRENT rules, replayed on the path)."""
    from .quant import parse_since
    if (r.get("start_ts") or 0) < parse_since(cfg.quant.learn_since):
        return None
    mint = str(r.get("mint") or "")
    if getattr(cfg.scout, "venues", "") == "pumpfun" and mint and not mint.endswith("pump"):
        return None
    try:
        path = [tuple(p) for p in json.loads(r.get("path") or "[]")]
    except (TypeError, ValueError):
        return None
    if not path:
        return None
    from .controller import path_problem
    if path_problem(path[:200]) == "frozen" or max(v for _, v in path) > 5000:
        return None
    cost = float(r.get("cost_pct") or 2.5)
    res = judge(path, cost, cfg)
    return {"ts": r.get("end_ts") or r.get("start_ts"), "mode": cfg.mode, "mint": mint, "symbol": r.get("symbol"),
            "score": r.get("score"), "label": res["label"], "ret_net_pct": res["ret_net"], "rt_cost_pct": cost,
            "gross_pct": res["ret_net"] + cost, "p_up": None, "p_down": None, "p_neither": None, "ev_pct": None,
            "weight": cfg.quant.shadow_weight, "source": "real"}


def closed_rows(store, stage: str = "analyst", untraded: bool = True) -> list[dict]:
    try:
        sql = "SELECT * FROM realwatch WHERE done=1 AND stage=?" + (" AND traded=0" if untraded else "")
        return [dict(r) for r in store.q(sql, (stage,))]
    except Exception:  # noqa: BLE001 - table not there yet
        return []


def load_paths(db, stage: str = "analyst") -> dict[tuple[str, int], dict]:
    """(mint, start second) -> closed realwatch row, for the Controller (read-only connection `db`)."""
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='realwatch'").fetchone():
            return {}
        return {(r["mint"], int(r["start_ts"])): dict(r) for r in db.execute(
            "SELECT mint,start_ts,path,label,ret_net,end_reason FROM realwatch WHERE done=1 AND stage=?", (stage,))}
    except Exception:  # noqa: BLE001
        return {}


def active_summary(store) -> dict:
    try:
        r = store.q("SELECT SUM(done=0) AS active, SUM(done=1) AS closed, SUM(done=1 AND label='up') AS up, "
                    "SUM(done=1 AND label='down') AS down FROM realwatch")[0]
        return {k: int(r[k] or 0) for k in ("active", "closed", "up", "down")}
    except Exception:  # noqa: BLE001
        return {"active": 0, "closed": 0, "up": 0, "down": 0}
