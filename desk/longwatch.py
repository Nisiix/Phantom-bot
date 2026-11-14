"""LONG WATCH — what happened to the tokens the desk turned down, 6 and 24 hours later (user 2026-11-14).

The 60-minute follow-up (desk/shadow.py) says what a buy would have earned with the desk's exits. It cannot say
whether a rule keeps out tokens that later reach the user's $100k line and stay alive, or only tokens that pump
for 25 minutes and die. This module follows every graduated token that was buyable by size (market cap and
liquidity above the Filter's minimum, in scope) and was rejected, by reading its market cap every few minutes:

  * peak market cap (only readings backed by real liquidity: a dust pool can show any price);
  * market cap and liquidity 6 h and 24 h after the first rejection.

The Controller's report then shows, for every rule: how many tokens it stopped, how many reached the line with a
real gain from the price at rejection, how many were still alive later. Measurement only: nothing here changes a
rule, and the rows are learning data (never deleted by a reset)."""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS longwatch (
  mint TEXT PRIMARY KEY, symbol TEXT, start_ts REAL, grad_ts REAL, decision TEXT, rules TEXT,
  ref_price REAL, ref_mcap REAL, ref_liq REAL, peak_mcap REAL, peak_price REAL, peak_ts REAL,
  mcap_6h REAL, liq_6h REAL, mcap_24h REAL, liq_24h REAL, last_ts REAL, last_mcap REAL, last_liq REAL,
  done INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_longwatch_done ON longwatch(done);
"""

# Failing one of these means "not our kind of token": not a missed chance, not followed.
OUT_OF_SCOPE = {"market", "venue", "token_age", "market_cap", "min_market_cap", "liquidity", "quote_currency",
                "rugcheck"}
H6, H24 = 6 * 3600, 24 * 3600
MIN_GROUP = 15            # fewest decided tokens a rule needs before its numbers are judged


class LongWatch:
    def __init__(self, desk) -> None:
        self.desk = desk
        self.c = desk.cfg.longwatch
        self.store = desk.store
        self.store.db.executescript(SCHEMA)
        # mint -> set of rule names already stored (only tokens still being followed)
        self.rules: dict[str, set[str]] = {
            r["mint"]: set(filter(None, (r["rules"] or "").split(",")))
            for r in self.store.q("SELECT mint, rules FROM longwatch WHERE done=0")}
        self.seen_done: set[str] = set()
        self.polls = 0

    # ── called by the agents ──
    def note(self, mint: str, symbol: str, snap, failed: list[str], kill: bool = False,
             now: float | None = None) -> bool:
        """A token was turned down. Returns True when it is (now) being followed."""
        if not self.c.enabled or snap is None or not failed:
            return False
        names = set(failed)
        known = self.rules.get(mint)
        if known is not None:
            if not names <= known or kill:
                known |= names
                self.store.db.execute(
                    "UPDATE longwatch SET rules=?, decision=CASE WHEN ? THEN 'kill' ELSE decision END WHERE mint=?",
                    (",".join(sorted(known)), int(kill), mint))
            return True
        if mint in self.seen_done or names & OUT_OF_SCOPE or len(self.rules) >= self.c.max_active:
            return False
        if (snap.dex_id or "").lower() != "pumpswap" or not snap.pair_created_at:
            return False            # not graduated yet: nothing the desk could have bought
        mcap = snap.market_cap or snap.fdv
        f = self.desk.cfg.filter
        if mcap < max(f.min_market_cap_usd, 1) or snap.liquidity_usd < f.min_liquidity_usd or snap.price_usd <= 0:
            return False
        if self.store.q("SELECT 1 FROM longwatch WHERE mint=?", (mint,)):
            self.seen_done.add(mint)    # followed once already (a finished row): one row per token
            return False
        t = now or time.time()
        self.rules[mint] = names
        self.store.db.execute(
            "INSERT INTO longwatch(mint,symbol,start_ts,grad_ts,decision,rules,ref_price,ref_mcap,ref_liq,"
            "peak_mcap,peak_price,peak_ts,last_ts,last_mcap,last_liq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mint, symbol, t, snap.pair_created_at, "kill" if kill else "reject", ",".join(sorted(names)),
             snap.price_usd, mcap, snap.liquidity_usd, mcap, snap.price_usd, t, t, mcap, snap.liquidity_usd))
        return True

    # ── follow-up ──
    def update(self, snaps: dict, now: float | None = None) -> int:
        """Apply one round of market readings. Returns how many tokens were closed (24 h reached)."""
        t = now or time.time()
        closed = 0
        for r in self.store.q("SELECT * FROM longwatch WHERE done=0"):
            mint, age = r["mint"], t - r["start_ts"]
            s = snaps.get(mint)
            mcap = (s.market_cap or s.fdv) if s else 0.0
            liq = s.liquidity_usd if s else 0.0
            price = s.price_usd if s else 0.0
            sets, vals = ["last_ts=?", "last_mcap=?", "last_liq=?"], [t, mcap, liq]
            # A peak counts only with real liquidity behind it, and not when the reading is a glitch
            # (market cap up but liquidity far below the first reading = a dust pool became the main pair).
            if s and mcap > (r["peak_mcap"] or 0) and liq >= max(self.c.min_peak_liquidity_usd,
                                                              0.25 * (r["ref_liq"] or 0)):
                sets += ["peak_mcap=?", "peak_price=?", "peak_ts=?"]
                vals += [mcap, price, t]
            if age >= H6 and r["mcap_6h"] is None:
                sets += ["mcap_6h=?", "liq_6h=?"]
                vals += [mcap, liq]
            if age >= H24:
                sets += ["mcap_24h=?", "liq_24h=?", "done=1"]
                vals += [mcap, liq]
                self.rules.pop(mint, None)
                closed += 1
            self.store.db.execute(f"UPDATE longwatch SET {','.join(sets)} WHERE mint=?", (*vals, mint))
        return closed

    async def loop(self) -> None:
        import asyncio
        if not self.c.enabled:
            return
        while not self.desk.stop.is_set():
            try:
                mints = list(self.rules)
                if mints:
                    snaps = await self.desk.dex.snapshots(mints, fresh=True, lane="longwatch")
                    self.update(snaps or {})
                    self.polls += 1
            except Exception:  # noqa: BLE001 - a missed reading is caught up by the next one
                pass
            try:
                await asyncio.wait_for(self.desk.stop.wait(), timeout=max(60.0, self.c.poll_seconds))
            except asyncio.TimeoutError:
                pass


# ═════════════════════════════════ report (Controller, read-only) ═════════════════════════════════
def load(db_path) -> list[dict]:
    """Followed tokens, without the ones the desk bought in the end (those were not missed)."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='longwatch'").fetchone():
            return []
        bought: set[str] = set()
        for q in ("SELECT mint FROM trades", "SELECT mint FROM trades_archive", "SELECT mint FROM positions"):
            try:
                bought |= {r[0] for r in db.execute(q)}
            except sqlite3.Error:
                pass
        return [dict(r) for r in db.execute("SELECT * FROM longwatch") if r["mint"] not in bought]
    finally:
        db.close()


def reached(r: dict, win_mcap: float, min_gain_pct: float) -> bool:
    """Reached the line AND paid: the peak is at least min_gain_pct above the price at rejection."""
    return ((r["peak_mcap"] or 0) >= win_mcap and (r["ref_price"] or 0) > 0
            and (r["peak_price"] or 0) >= r["ref_price"] * (1 + min_gain_pct / 100))


def alive(mcap: float | None, liq: float | None, c) -> bool:
    return (mcap or 0) >= c.alive_mcap_usd and (liq or 0) >= c.alive_liquidity_usd


def summary(rows: list[dict], cfg, now: float | None = None) -> dict:
    """Per rule: tokens stopped, reached the line, alive at 6 h / 24 h; the same for tokens stopped by that
    rule alone (the graduation wait is ignored there: nearly every token meets it first)."""
    t = now or time.time()
    c, win = cfg.longwatch, cfg.smart_wallets.win_mcap_usd

    def cell() -> dict:
        return {"n": 0, "n6": 0, "reached": 0, "alive6": 0, "n24": 0, "alive24": 0}

    def add(g: dict, r: dict) -> None:
        g["n"] += 1
        if t - r["start_ts"] < H6 or r["mcap_6h"] is None:
            return                              # too young to judge
        g["n6"] += 1
        g["reached"] += reached(r, win, c.min_gain_pct)
        g["alive6"] += alive(r["mcap_6h"], r["liq_6h"], c)
        if r["done"]:
            g["n24"] += 1
            g["alive24"] += alive(r["mcap_24h"], r["liq_24h"], c)

    total, rules, alone = cell(), {}, {}
    for r in rows:
        names = [n for n in (r["rules"] or "").split(",") if n]
        add(total, r)
        for n in names:
            add(rules.setdefault(n, cell()), r)
        solo = [n for n in names if n != "graduation_wait"] or names
        if len(solo) == 1:
            add(alone.setdefault(solo[0], cell()), r)
    return {"total": total, "rules": rules, "alone": alone, "win_mcap": win}


NAMES = {"graduation_wait": "attesa dopo la graduazione", "top10_holders": "top 10 troppo concentrati",
         "dev_sold": "vendite dello sviluppatore", "volume_1h": "volume orario basso",
         "rugged_name": "nome di un token finito in rug", "funder_rug_link": "finanziatore legato a un rug",
         "dev_rug_link": "sviluppatore legato a un rug", "holder_spread": "supply sparsa su wallet finti",
         "lp_locked": "liquidità non bloccata", "rugcheck_danger": "rischio grave segnalato da RugCheck",
         "mint_revoked": "mint non revocato", "freeze_revoked": "freeze non revocato",
         "token2022_safe": "estensioni pericolose", "analyst_score": "punteggio dell'Analyst sotto soglia"}


def report_section(s: dict | None, cfg) -> str:
    c = cfg.longwatch
    lines = ["", "", "## Token scartati — dove sono arrivati dopo 6 e 24 ore",
             f"Ogni token graduato, comprabile per dimensione e scartato viene seguito per 24 ore. "
             f"«Arrivato» = ha toccato ${cfg.smart_wallets.win_mcap_usd / 1000:.0f}k di market cap con almeno "
             f"+{c.min_gain_pct:g}% dal prezzo dello scarto. «Vivo» = market cap almeno "
             f"${c.alive_mcap_usd / 1000:.0f}k e liquidità almeno ${c.alive_liquidity_usd / 1000:.0f}k. "
             f"Solo misura: nessuna regola cambia da sola."]
    if not s or not s["total"]["n"]:
        return "\n".join(lines + ["- nessun token seguito ancora"])
    tot = s["total"]
    lines.append(f"- seguiti {tot['n']} token, {tot['n6']} con almeno 6 ore di storia, {tot['n24']} con 24 ore")
    if not tot["n6"]:
        return "\n".join(lines + ["- servono almeno 6 ore di dati per i primi numeri"])

    def row(name: str, g: dict) -> str:
        if not g["n6"]:
            return f"- {name}: {g['n']} token, nessuno ancora giudicabile"
        txt = (f"- {name}: {g['n6']} token, arrivati {g['reached']} ({100 * g['reached'] / g['n6']:.0f}%), "
               f"vivi a 6 ore {g['alive6']} ({100 * g['alive6'] / g['n6']:.0f}%)")
        if g["n24"]:
            txt += f", vivi a 24 ore {g['alive24']} su {g['n24']} ({100 * g['alive24'] / g['n24']:.0f}%)"
        return txt + ("" if g["n6"] >= MIN_GROUP else f" (pochi dati: servono almeno {MIN_GROUP} token)")

    lines.append(row("tutti gli scartati", tot))
    lines.append("### Per regola (un token può essere fermato da più regole)")
    for n, g in sorted(s["rules"].items(), key=lambda kv: -kv[1]["n"]):
        lines.append(row(NAMES.get(n, n), g))
    if s["alone"]:
        lines.append("### Fermati da una sola regola (oltre all'attesa)")
        for n, g in sorted(s["alone"].items(), key=lambda kv: -kv[1]["n"]):
            lines.append(row(NAMES.get(n, n), g))
        lines.append("- lettura: una regola che ferma molti token «vivi a 24 ore» costa occasioni; una che ferma "
                     "quasi solo token morti sta facendo il suo lavoro.")
    return "\n".join(lines)
