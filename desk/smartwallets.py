"""SMART WALLETS — who keeps buying the winners early (user 2026-11-12). Internal and free: plain Solana RPC for the
trades, the desk's own DexScreener readings for the market cap. No paid API, no wallet-label service.

1. EARLY BUYERS. Every pump.fun token the desk follows (shadow follow-ups, open positions) is read once from the
   chain: its oldest signatures give the create transaction (creator, slot) and the first `scan_trades` trades.
   Every wallet whose token balance changed is stored in `sw_trades` (learning data, never deleted) with:
     * signer   - the wallet signed the trade itself (a bundle distributing to fresh wallets does not),
     * bundle   - bought in the same slot as the create transaction (the creator's own bundle: measured on MEVS,
                  ~20 wallets in the first second, all the creator's),
     * creator  - the creator itself.
   Past tokens followed by the desk are backfilled in the background, the ones that crossed the win line first.
2. WINNERS. A token "won" when its market cap reached `win_mcap_usd` ($100k, user 2026-11-12; was $200k). Peaks come from the desk's own
   Filter readings and from a DexScreener check every `peak_check_minutes` for `peak_track_hours`; a reading counts
   only with real liquidity (>= $10k and market cap <= 25x liquidity: dust pools fake huge caps, BERNIE 2026-11-11).
   A token that did not win within `decide_hours` is a loss.
3. SCORE. For every wallet, over the decided tokens it bought early (signer, not bundle, not creator): tokens, wins,
   hit rate. A wallet is "smart" with >= min_wins wins, >= min_tokens tokens and a hit rate >= min_hit_rate: a sniper
   that buys everything has many wins but a low hit rate, a creator's fresh bundle wallet never repeats.
4. WATCH. For the tokens the Analyst is following and the open positions, the smart wallets' token accounts are
   read in one getMultipleAccounts call (100 wallets each) every `watch_seconds`. A smart wallet holding the token
   is stored in `sw_hits` and shown as an event.
5. REPORT TO THE ANALYST (user 2026-11-12). For every token the Analyst scores, the Whale checker reads the smart
   holders on the spot and reports them. Only when the data is sufficient (readiness(): enough decided tokens,
   winners and smart wallets) does the Analyst use it: one smart holder = 51% of its score ([analyst] whale_weight);
   otherwise it ignores it. The report also carries the smart wallets' own average buy price (read from their token
   account): the Executor buys only within +30% of it and drops the token if they sell (desk/whale_entry.py).
   Nothing here buys or sells by itself: the Analyst's caps and the Executor's checks still decide.

SAFEGUARDS (devil's-advocate review, 2026-11-12):
  * dust buys don't count: an early buy under `min_buy_sol` (0.1 SOL; volume/spam bots buy 0.001-0.01 SOL);
  * real money only (user 2026-11-12): a smart wallet must hold at least `min_balance_usd` ($500) in SOL + USDC +
    USDT right now (read on-chain every rescore, one getMultipleAccounts per 100 wallets); below that it is never
    counted and NOT stored (user 2026-11-12: no space spent on them; its balance is kept in memory only);
  * one operator = one win: wins are counted on distinct creators (their funder when known), so a pump group
    relaunching through fresh creators with the same wallets does not collect a "win" per relaunch;
  * no self-proof: a token never counts as proof for itself - a holder is smart for THIS token only if it is still
    smart with this token's own result taken out (a token already past the win line made its early buyers "smart");
  * insiders don't count: the creator, its DevWatch group, wallets funded by the creator or by the creator's funder
    are never counted as smart holders of that token;
  * one operator = one holder: smart wallets funded by the same (non-exchange) wallet count once (funders looked up
    on-chain in the background, cached in `wallet_funders`);
  * sellers don't count: a smart wallet that already sold half of the largest amount seen is not a holder any more;
  * proof before use: "sufficient data" also needs the list to have proved itself on tokens it had not seen
    (validate(): wallets chosen on the older 70% of the decided tokens must pick winners on the newer 30% at least
    `ready_min_lift` times the base rate, on >= `ready_min_validation_tokens` tokens);
  * RPC budget: holders are cached 60 s; open positions are watched every minute, the Analyst's tokens every
    `watch_analyst_seconds` (the Analyst asks again itself at every re-score).
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections import deque

from .devwatch import SPL_TOKEN, ata
from .funding import Funding


class _Paced:
    """RPC adapter for desk/funding.py: its calls go through the Whale checker's paced lane."""

    def __init__(self, sw: "SmartWallets") -> None:
        self.sw = sw

    async def call(self, method: str, params: list | None = None):
        return await self.sw.call(method, params or [])

SCHEMA = """
CREATE TABLE IF NOT EXISTS sw_tokens (
  mint TEXT PRIMARY KEY, symbol TEXT, creator TEXT, create_ts REAL, create_slot INTEGER, scanned_ts REAL,
  complete INTEGER, n_trades INTEGER, peak_mcap REAL, peak_ts REAL, peak_checked REAL, won INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS sw_trades (
  mint TEXT, wallet TEXT, sig TEXT, slot INTEGER, ts REAL, idx INTEGER, side TEXT, tokens REAL, sol REAL,
  signer INTEGER, bundle INTEGER, creator INTEGER, PRIMARY KEY (sig, wallet, mint));
CREATE INDEX IF NOT EXISTS sw_trades_wallet ON sw_trades(wallet);
CREATE INDEX IF NOT EXISTS sw_trades_mint ON sw_trades(mint);
CREATE TABLE IF NOT EXISTS sw_smart (wallet TEXT PRIMARY KEY, tokens INTEGER, wins INTEGER, hit_rate REAL,
  sol_in REAL, last_ts REAL, ts REAL);
CREATE INDEX IF NOT EXISTS ix_dec_mint ON decisions(mint);
CREATE TABLE IF NOT EXISTS sw_hits (mint TEXT, wallet TEXT, symbol TEXT, ts REAL, stage TEXT, held_at_start INTEGER,
  tokens REAL, wins INTEGER, n INTEGER, PRIMARY KEY (mint, wallet));
"""

# Stablecoins counted in a smart wallet's balance (SPL Token program, 6 decimals).
STABLES = ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")   # USDT

_MC = re.compile(r"market cap \$([\d,]+)")
_LIQ = re.compile(r"liquidity: ([\d.]+) vs")


def real_cap(mcap: float, liq: float, min_liq: float = 10_000.0, max_ratio: float = 25.0) -> bool:
    """A market cap reading backed by a real pool (dust pools show caps of millions on a few dollars)."""
    return mcap > 0 and liq >= min_liq and mcap / liq <= max_ratio


def parse_trades(tx: dict, mint: str) -> list[dict]:
    """Every wallet whose balance of `mint` changed in a parsed transaction: wallet, side, tokens, sol, signer.
    A non-signer whose balance fell is the pool / bonding curve paying out: skipped."""
    if not tx or (tx.get("meta") or {}).get("err"):
        return []
    meta = tx.get("meta") or {}
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    keys = [k if isinstance(k, dict) else {"pubkey": k, "signer": i == 0} for i, k in enumerate(keys)]
    index = {k.get("pubkey"): i for i, k in enumerate(keys)}
    signers = {k.get("pubkey") for k in keys if k.get("signer")}

    def bal(rows):
        out: dict[str, float] = {}
        for b in rows or []:
            if b.get("mint") != mint or not b.get("owner"):
                continue
            ui = b.get("uiTokenAmount") or {}
            out[b["owner"]] = out.get(b["owner"], 0.0) + float(ui.get("uiAmountString") or ui.get("uiAmount") or 0)
        return out

    pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    pre_sol, post_sol = meta.get("preBalances") or [], meta.get("postBalances") or []
    out = []
    for owner in set(pre) | set(post):
        d = post.get(owner, 0.0) - pre.get(owner, 0.0)
        if abs(d) < 1e-9:
            continue
        signer = owner in signers
        if not signer and d < 0:
            continue
        i = index.get(owner)
        sol = 0.0
        if i is not None and i < len(pre_sol) and i < len(post_sol):
            sol = abs(post_sol[i] - pre_sol[i]) / 1e9
        out.append({"wallet": owner, "side": "buy" if d > 0 else "sell", "tokens": abs(d), "sol": sol,
                    "signer": signer})
    return out


class SmartWallets:
    def __init__(self, store, rpc, dex, cfg, emit=None) -> None:
        self.store, self.rpc, self.dex, self.c = store, rpc, dex, cfg
        self.emit = emit or (lambda *a, **k: None)
        store.db.executescript(SCHEMA)
        self._last_call = 0.0
        self._programs: dict[str, str] = {}
        self._held: dict[str, set] = {}
        for r in store.q("SELECT mint, wallet FROM sw_hits"):
            self._held.setdefault(r["mint"], set()).add(r["wallet"])
        try:
            store.db.execute("ALTER TABLE sw_smart ADD COLUMN balance_usd REAL")
        except Exception:  # noqa: BLE001 - column already there
            pass
        rows = store.q("SELECT * FROM sw_smart")
        self._balance: dict[str, float] = {r["wallet"]: r["balance_usd"] for r in rows if r.get("balance_usd") is not None}
        self.all_smart: dict[str, dict] = {r["wallet"]: r for r in rows}   # before the balance check
        self.smart: dict[str, dict] = {w: r for w, r in self.all_smart.items() if self.balance_ok(w)}
        # The win line can change ([smart_wallets] win_mcap_usd): every stored token is judged again on the line in
        # use (its peak is kept, only the verdict follows the line).
        store.db.execute("UPDATE sw_tokens SET won = CASE WHEN COALESCE(peak_mcap, 0) >= ? THEN 1 ELSE 0 END",
                         (float(cfg.win_mcap_usd),))
        self._backfill: list[str] = []
        self._backfill_ts = 0.0
        self._bot_peaks: dict[str, tuple[float, float]] = {}
        self._fails: dict[str, int] = {}
        self._ready: dict = {}
        self._lock = asyncio.Lock()
        self._scanned: set[str] = {r["mint"] for r in store.q("SELECT mint FROM sw_tokens")}
        self._contrib: dict[str, dict[str, tuple[int, str]]] = {}   # smart wallet -> mint -> (won, operator)
        self._hcache: dict[str, tuple[float, dict]] = {}
        self._max_amt: dict[tuple[str, str], float] = {}
        self._validation: dict = {}
        self._entries: dict[tuple[str, str], tuple[float, float | None]] = {}   # (mint, wallet) -> (ts, SOL/token)
        self.reports: dict[str, dict] = {}          # mint -> last report to the Analyst (full wallets, for the Executor)
        self.funding = Funding(store, _Paced(self), hops=1)
        self.stats = {"scanned": 0, "too_long": 0, "trades": 0, "errors": 0, "hits": 0, "backfilled": 0,
                      "reports": 0}
        # How well the harvest works (read by the Controller, desk/whale_audit.py harvest checks)
        self.calls = 0                              # RPC calls made by this engine since the start
        self.started = time.time()
        self._scans: deque = deque(maxlen=600)      # one entry per token read (or failed)
        self._reps: deque = deque(maxlen=600)       # one entry per report to the Analyst
        self.backlog = 0                            # followed tokens not read yet

    # ── RPC, paced: the public node is shared with the Filter, DevWatch and the funder backfill ──
    async def call(self, method: str, params: list):
        async with self._lock:
            wait = self._last_call + float(self.c.rpc_interval_seconds) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.time()
            self.calls += 1
        return await self.rpc.call(method, params, lane="smart")

    # ── 1. early buyers ──
    def scanned(self, mint: str) -> bool:
        return mint in self._scanned

    async def scan(self, mint: str, symbol: str = "", source: str = "follow", max_pages: int | None = None) -> dict:
        """Read the create transaction and the first trades of `mint` from the chain and store them."""
        pages = int(max_pages or self.c.max_sig_pages)
        t0, c0, used = time.time(), self.calls, 0
        before, oldest, complete = None, [], False
        for _ in range(max(1, pages)):
            used += 1
            opt: dict = {"limit": 1000}
            if before:
                opt["before"] = before
            sigs = await self.call("getSignaturesForAddress", [mint, opt]) or []
            if sigs:
                oldest = sigs
            if len(sigs) < 1000:
                complete = True
                break
            before = sigs[-1]["signature"]
        row = {"mint": mint, "symbol": symbol, "creator": None, "create_ts": None, "create_slot": None,
               "complete": int(complete and bool(oldest)), "n_trades": 0, "source": source}
        if not row["complete"]:
            self.stats["too_long"] += 1
            self._save_token(row)
            self._scans.append({"t": time.time(), "secs": time.time() - t0, "calls": self.calls - c0, "pages": used,
                                "complete": False, "trades": 0, "source": source})
            return row
        sigs = list(reversed(oldest))                    # oldest first: [0] is the create transaction
        create = sigs[0]
        row["create_ts"], row["create_slot"] = create.get("blockTime"), create.get("slot")
        tx = await self.call("getTransaction", [create["signature"], {"encoding": "jsonParsed",
                                                                      "maxSupportedTransactionVersion": 1}])
        keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        if keys:
            k = keys[0]
            row["creator"] = k.get("pubkey") if isinstance(k, dict) else k
        # Only USABLE trades use up the scan_trades budget (a buy of >= min_buy_sol signed by a wallet that is not the
        # creator, outside the creator's same-slot bundle): measured 2026-11-12 on the first token read after the
        # restart, 32 of 64 rows were dust buys and 22 the creator's bundle, 6 usable. Dust buys are not even stored
        # (no space spent on them, user 2026-11-12). At most 2 x scan_trades transactions are read.
        rows, idx, reads, useful, n_dust, n_bundle = [], 0, 0, 0, 0, 0
        min_sol = float(self.c.min_buy_sol)
        for s in sigs[1:]:
            if useful >= int(self.c.scan_trades) or reads >= 2 * int(self.c.scan_trades):
                break
            if s.get("err"):
                continue
            reads += 1
            tx = await self.call("getTransaction", [s["signature"], {"encoding": "jsonParsed",
                                                                     "maxSupportedTransactionVersion": 1}])
            trades = parse_trades(tx, mint)
            if not trades:
                continue
            bundle = int(s.get("slot") is not None and s.get("slot") == row["create_slot"])
            usable = False
            for t in trades:
                creator = t["wallet"] == row["creator"]
                if bundle:
                    n_bundle += 1
                elif t["side"] == "buy" and t["signer"] and not creator:
                    if t["sol"] < min_sol:
                        n_dust += 1
                        continue                          # dust: not stored
                    usable = True
                rows.append((mint, t["wallet"], s["signature"], s.get("slot"), s.get("blockTime"), idx, t["side"],
                             t["tokens"], t["sol"], int(t["signer"]), bundle, int(creator)))
            if not bundle:
                idx += 1
            if usable:
                useful += 1
        self.store.db.executemany("INSERT OR IGNORE INTO sw_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        row["n_trades"] = idx
        self._save_token(row)
        self.stats["scanned"] += 1
        self.stats["trades"] += idx
        self._scans.append({"t": time.time(), "secs": time.time() - t0, "calls": self.calls - c0, "pages": used,
                            "complete": True, "trades": idx, "reads": reads, "usable": useful, "dust": n_dust,
                            "bundle": n_bundle, "source": source})
        return row

    def _save_token(self, r: dict) -> None:
        peak, pts = self.bot_peak(r["mint"])
        won = int(peak >= float(self.c.win_mcap_usd))
        self.store.db.execute(
            "INSERT OR REPLACE INTO sw_tokens(mint,symbol,creator,create_ts,create_slot,scanned_ts,complete,n_trades,"
            "peak_mcap,peak_ts,peak_checked,won,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["mint"], r.get("symbol") or "", r.get("creator"), r.get("create_ts"), r.get("create_slot"),
             time.time(), r["complete"], r.get("n_trades") or 0, peak, pts, 0.0, won, r.get("source") or ""))
        self._scanned.add(r["mint"])

    # ── 2. winners ──
    def bot_peaks(self, q=None) -> dict[str, tuple[float, float]]:
        """Highest real market cap the Filter ever read for each pump mint: mint -> (mcap, ts)."""
        out: dict[str, tuple[float, float]] = {}
        for r in (q or self.store.q)("SELECT ts, mint, data FROM decisions WHERE mint LIKE '%pump' "
                              "AND data LIKE '%market cap $%'"):
            m, liq = _MC.search(r["data"] or ""), _LIQ.search(r["data"] or "")
            if not m or not liq:
                continue
            mc, lq = float(m.group(1).replace(",", "")), float(liq.group(1))
            if real_cap(mc, lq) and mc > out.get(r["mint"], (0.0, 0.0))[0]:
                out[r["mint"]] = (mc, r["ts"])
        return out

    def bot_peak(self, mint: str) -> tuple[float, float]:
        if mint in self._bot_peaks:
            return self._bot_peaks[mint]
        best = (0.0, 0.0)
        for r in self.store.q("SELECT ts, data FROM decisions WHERE mint=? AND data LIKE '%market cap $%'", (mint,)):
            m, liq = _MC.search(r["data"] or ""), _LIQ.search(r["data"] or "")
            if m and liq:
                mc = float(m.group(1).replace(",", ""))
                if real_cap(mc, float(liq.group(1))) and mc > best[0]:
                    best = (mc, r["ts"])
        return best

    async def track_peaks(self) -> int:
        """DexScreener check of the recent, undecided tokens. Returns how many readings moved a peak up."""
        t = time.time()
        # Every followed token, also the ones whose history was too long to read (create_ts unknown: scanned_ts),
        # so the Controller can measure the Whale checker's precision on all of them (desk/whale_audit.py).
        since = t - float(self.c.peak_track_hours) * 3600
        rows = self.store.q("SELECT mint, peak_mcap FROM sw_tokens WHERE won=0 AND COALESCE(create_ts, scanned_ts) > ? "
                            "AND peak_checked < ? ORDER BY peak_checked LIMIT 300",
                            (since, t - float(self.c.peak_check_minutes) * 60))
        if not rows:
            return 0
        snaps = await self.dex.snapshots([r["mint"] for r in rows], fresh=True, lane="smart") or {}
        n = 0
        for r in rows:
            s = snaps.get(r["mint"])
            peak = float(r["peak_mcap"] or 0)
            mc = float(getattr(s, "market_cap", 0) or getattr(s, "fdv", 0) or 0) if s else 0.0
            if s and real_cap(mc, float(s.liquidity_usd or 0)) and mc > peak:
                peak, n = mc, n + 1
                self.store.db.execute("UPDATE sw_tokens SET peak_mcap=?, peak_ts=?, won=? WHERE mint=?",
                                      (peak, t, int(peak >= float(self.c.win_mcap_usd)), r["mint"]))
            self.store.db.execute("UPDATE sw_tokens SET peak_checked=? WHERE mint=?", (t, r["mint"]))
        return n

    # ── 3. score ──
    _BUYS = ("SELECT mint, wallet, SUM(sol) AS sol, MIN(ts) AS ts FROM sw_trades WHERE side='buy' AND signer=1 "
             "AND bundle=0 AND creator=0 GROUP BY mint, wallet HAVING SUM(sol) >= ?")
    _DECIDED = ("SELECT t.mint, t.won, COALESCE(f.funder, t.creator, t.mint) AS op FROM sw_tokens t "
                "LEFT JOIN wallet_funders f ON f.wallet = t.creator AND f.hub = 0 AND f.funder IS NOT NULL "
                "WHERE t.complete=1 AND (t.won=1 OR t.create_ts < ?)")

    def _ro(self, sql: str, args: tuple = ()) -> list[dict]:
        """Read-only query on its own connection: the heavy reads run in a worker thread, never in the event loop
        (a few seconds there would delay Risk's stop checks)."""
        db = sqlite3.connect(f"file:{self.store.path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(sql, args)]
        finally:
            db.close()

    def rescore(self, now: float | None = None) -> dict[str, dict]:
        """Smart wallets: early buys (signer, not bundle, not creator, >= min_buy_sol) on decided tokens; wins are
        counted once per operator (creator's funder, or the creator), the hit rate is wins / tokens."""
        now = now or time.time()
        self._apply(now, *self._compute(now, self.store.q))
        return self.smart

    async def rescore_async(self, now: float | None = None) -> dict[str, dict]:
        """rescore() with the reads in a worker thread (the desk's loop keeps running meanwhile)."""
        now = now or time.time()
        res = await asyncio.to_thread(self._compute, now, self._ro)
        self._apply(now, *res)
        return self.smart

    def _compute(self, now: float, q) -> tuple[list[dict], dict, dict]:
        c = self.c
        args = (float(c.min_buy_sol), now - float(c.decide_hours) * 3600)
        rows = q(
            f"WITH b AS ({self._BUYS}), d AS ({self._DECIDED}) "
            "SELECT b.wallet, COUNT(*) AS tokens, COUNT(DISTINCT CASE WHEN d.won=1 THEN d.op END) AS wins, "
            "SUM(b.sol) AS sol_in, MAX(b.ts) AS last_ts FROM b JOIN d ON d.mint = b.mint GROUP BY b.wallet "
            "HAVING wins >= ? AND tokens >= ?", args + (int(c.min_wins), int(c.min_tokens)))
        smart = {}
        for r in rows:
            hr = (r["wins"] or 0) / max(1, r["tokens"])
            if hr >= float(c.min_hit_rate):
                smart[r["wallet"]] = {**r, "hit_rate": round(hr, 3), "ts": now}
        ranked = sorted(smart.values(), key=lambda r: (-r["hit_rate"], -r["wins"]))[: int(c.max_smart_wallets)]
        contrib: dict[str, dict[str, tuple[int, str]]] = {r["wallet"]: {} for r in ranked}
        ws = list(contrib)
        for i in range(0, len(ws), 400):
            chunk = ws[i: i + 400]
            for r in q(f"WITH b AS ({self._BUYS}), d AS ({self._DECIDED}) SELECT b.wallet, b.mint, d.won, "
                                  f"d.op FROM b JOIN d ON d.mint = b.mint WHERE b.wallet IN "
                                  f"({','.join('?' * len(chunk))})", args + tuple(chunk)):
                contrib[r["wallet"]][r["mint"]] = (int(r["won"] or 0), r["op"])
        return ranked, contrib, self.validate(now, q)

    def _apply(self, now: float, ranked: list[dict], contrib: dict, validation: dict) -> None:
        self.store.db.execute("DELETE FROM sw_smart")       # derived list: rebuilt from sw_trades every time
        self.store.db.executemany("INSERT INTO sw_smart(wallet,tokens,wins,hit_rate,sol_in,last_ts,ts,balance_usd) "
                                  "VALUES(?,?,?,?,?,?,?,?)",
                                  [(r["wallet"], r["tokens"], r["wins"], r["hit_rate"], r["sol_in"] or 0.0,
                                    r["last_ts"], now, self._balance.get(r["wallet"])) for r in ranked
                                   if not self._poor(r["wallet"])])
        self.all_smart = {r["wallet"]: r for r in ranked}
        self.smart = {w: r for w, r in self.all_smart.items() if self.balance_ok(w)}
        self._contrib = contrib
        self._validation = validation
        self._ready = {}

    # ── real money only (user 2026-11-12) ──
    def _poor(self, wallet: str) -> bool:
        """Balance read and under min_balance_usd (an unread balance is not poor yet)."""
        need = float(self.c.min_balance_usd)
        b = self._balance.get(wallet)
        return need > 0 and b is not None and b < need

    def balance_ok(self, wallet: str) -> bool:
        """At least min_balance_usd in SOL + USDC + USDT at the last reading (unknown = not yet: not counted)."""
        need = float(self.c.min_balance_usd)
        if need <= 0:
            return True
        b = self._balance.get(wallet)
        return b is not None and b >= need

    async def refresh_balances(self, sol_usd: float | None = None) -> int:
        """Read every smart wallet's SOL + USDC + USDT now and keep only the ones worth >= min_balance_usd.
        Returns how many are counted. Without a SOL price the previous readings stay."""
        if float(self.c.min_balance_usd) <= 0 or not self.all_smart:
            self.smart = {w: r for w, r in self.all_smart.items() if self.balance_ok(w)}
            return len(self.smart)
        px = float(sol_usd if sol_usd is not None else (self.store.kv_get("sol_usd", 0.0) or 0.0))
        if px <= 0:
            return len(self.smart)
        ws = list(self.all_smart)
        usd = {w: 0.0 for w in ws}
        for i in range(0, len(ws), 100):
            chunk = ws[i: i + 100]
            res = await self.call("getMultipleAccounts", [chunk, {"encoding": "base64", "dataSlice": {"offset": 0,
                                                                                                      "length": 0}}])
            for w, acc in zip(chunk, (res or {}).get("value") or []):
                usd[w] += (float((acc or {}).get("lamports") or 0) / 1e9) * px
            for stable in STABLES:
                pairs = [(w, ata(w, stable, SPL_TOKEN)) for w in chunk]
                pairs = [(w, a) for w, a in pairs if a]
                res = await self.call("getMultipleAccounts", [[a for _, a in pairs], {"encoding": "jsonParsed"}])
                for (w, _), acc in zip(pairs, (res or {}).get("value") or []):
                    try:
                        ui = acc["data"]["parsed"]["info"]["tokenAmount"]
                        usd[w] += float(ui.get("uiAmountString") or ui.get("uiAmount") or 0)
                    except (KeyError, TypeError, ValueError):
                        continue
        self._balance.update(usd)
        if len(self._balance) > 20000:                    # memory only: keep the wallets still in the list
            self._balance = {w: v for w, v in self._balance.items() if w in self.all_smart}
        need = float(self.c.min_balance_usd)
        self.store.db.executemany("UPDATE sw_smart SET balance_usd=? WHERE wallet=?",
                                  [(v, w) for w, v in usd.items() if v >= need])
        # under the line: not stored at all (user 2026-11-12)
        self.store.db.executemany("DELETE FROM sw_smart WHERE wallet=?", [(w,) for w, v in usd.items() if v < need])
        before = len(self.smart)
        self.smart = {w: {**r, "balance_usd": usd.get(w)} for w, r in self.all_smart.items() if self.balance_ok(w)}
        if len(self.smart) != before:
            self._ready = {}
        return len(self.smart)

    @staticmethod
    def pick(buys: dict[str, set], tokens: dict[str, tuple[int, str]], c) -> set:
        """Smart wallets from {wallet: mints bought early} on `tokens` {mint: (won, operator)} (same rule as rescore)."""
        out = set()
        for w, ms in buys.items():
            ms = [m for m in ms if m in tokens]
            if len(ms) < int(c.min_tokens):
                continue
            wins = len({tokens[m][1] for m in ms if tokens[m][0]})
            if wins >= int(c.min_wins) and wins / len(ms) >= float(c.min_hit_rate):
                out.add(w)
        return out

    def validate(self, now: float | None = None, q=None) -> dict:
        """Out-of-sample check: wallets picked on the older 70% of the decided tokens (last lookback_days), scored
        on the newer 30%: share of the newer tokens with one of them among the early buyers that won, vs the base
        rate of all newer tokens."""
        now = now or time.time()
        q = q or self.store.q
        c = self.c
        since = now - float(c.validation_lookback_days) * 86400
        dec = q("SELECT t.mint, t.won, COALESCE(f.funder, t.creator, t.mint) AS op, t.create_ts FROM sw_tokens t "
                           "LEFT JOIN wallet_funders f ON f.wallet = t.creator AND f.hub = 0 AND f.funder IS NOT NULL "
                           "WHERE t.complete=1 AND t.create_ts > ? AND (t.won=1 OR t.create_ts < ?) ORDER BY t.create_ts",
                           (since, now - float(c.decide_hours) * 3600))
        out = {"n": 0, "wins": 0, "base": None, "precision": None, "lift": None, "test_tokens": 0}
        if len(dec) < 10:
            return out
        cut = int(len(dec) * 0.7)
        train = {r["mint"]: (int(r["won"] or 0), r["op"]) for r in dec[:cut]}
        test = {r["mint"]: int(r["won"] or 0) for r in dec[cut:]}
        buys: dict[str, set] = {}
        by_mint: dict[str, set] = {}
        for r in q(f"SELECT b.mint, b.wallet FROM ({self._BUYS}) b JOIN sw_tokens t ON t.mint = b.mint "
                              f"WHERE t.create_ts > ?", (float(c.min_buy_sol), since)):
            if r["mint"] in train:
                buys.setdefault(r["wallet"], set()).add(r["mint"])
            elif r["mint"] in test:
                by_mint.setdefault(r["mint"], set()).add(r["wallet"])
        picked = self.pick(buys, train, c)
        sig = [m for m, ws in by_mint.items() if ws & picked]
        out["test_tokens"] = len(test)
        out["base"] = sum(test.values()) / len(test)
        out["n"], out["wins"] = len(sig), sum(test[m] for m in sig)
        if sig:
            out["precision"] = out["wins"] / len(sig)
            out["lift"] = out["precision"] / out["base"] if out["base"] else (float("inf") if out["wins"] else 0.0)
        return out

    def smart_for(self, wallet: str, mint: str) -> dict | None:
        """The wallet's record if it is smart even with `mint`'s own result left out (no self-proof), else None."""
        s = self.smart.get(wallet)
        if s is None:
            return None
        con = self._contrib.get(wallet) or {}
        if mint not in con:
            return s
        rest = [v for m, v in con.items() if m != mint]
        wins = len({op for won, op in rest if won})
        c = self.c
        if wins < int(c.min_wins) or len(rest) < int(c.min_tokens) or wins / max(1, len(rest)) < float(c.min_hit_rate):
            return None
        return {**s, "tokens": len(rest), "wins": wins, "hit_rate": round(wins / len(rest), 3)}

    def operator(self, wallet: str) -> str:
        """Who is behind a wallet: its funder when known and not an exchange, else the wallet itself."""
        f = self.store.wallet_funder(wallet)
        return f["funder"] if f and f.get("funder") and not f.get("hub") else wallet

    async def lookup_funders(self, limit: int = 1) -> int:
        """Background: who funded the smart wallets (one at a time, cached in wallet_funders)."""
        n = 0
        for w in list(self.smart):
            if n >= limit:
                break
            if self.store.wallet_funder(w) is None:
                await self.funding.funder(w)
                n += 1
        return n

    # ── 4. watch ──
    async def token_program(self, mint: str) -> str:
        if mint not in self._programs:
            info = await self.call("getAccountInfo", [mint, {"encoding": "base64"}])
            self._programs[mint] = ((info or {}).get("value") or {}).get("owner") or SPL_TOKEN
        return self._programs[mint]

    async def holders(self, mint: str, max_age: float = 60.0) -> dict[str, float]:
        """Smart wallets holding `mint` right now -> UI amount (one RPC call per 100 wallets, cached max_age s)."""
        if not self.smart:
            return {}
        hit = self._hcache.get(mint)
        if hit and time.time() - hit[0] < max_age:
            return dict(hit[1])
        if len(self._hcache) > 2000:
            self._hcache.clear()
        prog = await self.token_program(mint)
        pairs = [(w, ata(w, mint, prog)) for w in self.smart]
        pairs = [(w, a) for w, a in pairs if a]
        out: dict[str, float] = {}
        for i in range(0, len(pairs), 100):
            chunk = pairs[i: i + 100]
            res = await self.call("getMultipleAccounts", [[a for _, a in chunk], {"encoding": "jsonParsed"}])
            for (w, _), acc in zip(chunk, (res or {}).get("value") or []):
                try:
                    ui = acc["data"]["parsed"]["info"]["tokenAmount"]
                    amt = float(ui.get("uiAmountString") or ui.get("uiAmount") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if amt > 0:
                    out[w] = amt
                    k = (mint, w)
                    self._max_amt[k] = max(self._max_amt.get(k, 0.0), amt)
        self._hcache[mint] = (time.time(), dict(out))
        return out

    async def entry_price(self, mint: str, wallet: str, max_txs: int = 6) -> float | None:
        """The wallet's average buy price for `mint` in SOL per token, read from its token account's history (a
        handful of transactions). None when it can't be read (no SOL spent seen, e.g. a buy paid in wrapped SOL).
        Cached 10 minutes."""
        hit = self._entries.get((mint, wallet))
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        prog = await self.token_program(mint)
        acc = ata(wallet, mint, prog)
        price = None
        if acc:
            sigs = await self.call("getSignaturesForAddress", [acc, {"limit": 50}]) or []
            sol = tok = 0.0
            for sg in [x for x in reversed(sigs) if not x.get("err")][:max_txs]:      # oldest first: the buys
                tx = await self.call("getTransaction", [sg["signature"], {"encoding": "jsonParsed",
                                                                          "maxSupportedTransactionVersion": 1}])
                for t in parse_trades(tx, mint):
                    if t["wallet"] == wallet and t["side"] == "buy" and t["signer"] and t["sol"] > 0:
                        sol, tok = sol + t["sol"], tok + t["tokens"]
            price = sol / tok if tok > 0 and sol > 0 else None
        if len(self._entries) > 5000:
            self._entries.clear()
        self._entries[(mint, wallet)] = (time.time(), price)
        return price

    async def sold(self, mint: str, wallets: list[str]) -> list[str]:
        """Which of these smart wallets sold `mint`: no longer hold it, or hold under half of the most seen."""
        if not wallets:
            return []
        held = await self.holders(mint)
        return [w for w in wallets if w not in held or self.selling(mint, w, held[w])]

    def selling(self, mint: str, wallet: str, amt: float) -> bool:
        """Already sold half of the largest amount we saw it hold."""
        return amt < 0.5 * self._max_amt.get((mint, wallet), amt)

    def record(self, mint: str, symbol: str, held: dict[str, float], stage: str, quiet: bool = False) -> list[str]:
        """Store the smart wallets holding a token (one row per wallet and token). Returns the new ones."""
        first_check = mint not in self._held
        seen = self._held.setdefault(mint, set())
        new = [w for w in held if w not in seen]
        for w in new:
            s = self.smart.get(w) or {}
            seen.add(w)
            self.store.db.execute("INSERT OR IGNORE INTO sw_hits VALUES(?,?,?,?,?,?,?,?,?)",
                                  (mint, w, symbol, time.time(), stage, int(first_check), held[w],
                                   s.get("wins") or 0, s.get("tokens") or 0))
            self.stats["hits"] += 1
            if not quiet:
                self.emit("info", f"smart wallet {w[:6]} {'holds' if first_check else 'bought'} {symbol or mint[:6]} "
                                  f"({s.get('wins', 0)}/{s.get('tokens', 0)} early buys reached "
                                  f"${float(self.c.win_mcap_usd) / 1000:.0f}k)",
                          mint, symbol, wallet=w, wins=s.get("wins"), tokens=s.get("tokens"))
        return new

    async def watch(self, mint: str, symbol: str = "", stage: str = "") -> list[str]:
        """Record the smart wallets that hold a followed token. Returns the wallets seen for the first time."""
        held = await self.holders(mint)
        return self.record(mint, symbol, {w: a for w, a in held.items() if self.smart_for(w, mint) is not None}, stage)

    # ── 5. report to the Analyst (user 2026-11-12) ──
    def readiness(self, now: float | None = None) -> dict:
        """Is there enough data for the Analyst to use the smart wallets? Cached for 5 minutes.
        ready = >= ready_min_decided_tokens tokens read and decided, >= ready_min_winners of them winners and
        >= ready_min_smart_wallets smart wallets. Below that the Analyst ignores the Whale checker."""
        now = now or time.time()
        if self._ready and now - self._ready.get("ts", 0) < 300:
            return self._ready
        c = self.c
        r = self.store.q("SELECT COUNT(*) AS n, COALESCE(SUM(won), 0) AS w FROM sw_tokens WHERE complete=1 "
                         "AND (won=1 OR create_ts < ?)", (now - float(c.decide_hours) * 3600,))[0]
        out = {"decided": int(r["n"]), "winners": int(r["w"]), "smart": len(self.smart), "ts": now}
        missing = []
        if out["decided"] < int(c.ready_min_decided_tokens):
            missing.append(f"{out['decided']}/{c.ready_min_decided_tokens} tokens decided")
        if out["winners"] < int(c.ready_min_winners):
            missing.append(f"{out['winners']}/{c.ready_min_winners} winners")
        if out["smart"] < int(c.ready_min_smart_wallets):
            missing.append(f"{out['smart']}/{c.ready_min_smart_wallets} smart wallets")
        v = self._validation or {}
        out["validation"] = v
        if int(v.get("n") or 0) < int(c.ready_min_validation_tokens):
            missing.append(f"out-of-sample check on {int(v.get('n') or 0)}/{c.ready_min_validation_tokens} tokens")
        elif (v.get("lift") or 0) < float(c.ready_min_lift):
            missing.append(f"out-of-sample lift {v.get('lift') or 0:.2f} < {c.ready_min_lift:g} (the list does not "
                           f"pick winners better than chance yet)")
        out["ready"] = not missing
        out["why"] = ("not enough data yet: " + ", ".join(missing)) if missing else \
            (f"{out['smart']} smart wallets from {out['decided']} tokens ({out['winners']} winners)"
             + (f", out-of-sample {v['wins']}/{v['n']} won vs base {100 * (v.get('base') or 0):.0f}% "
                f"(lift {v['lift']:.1f})" if v.get("lift") is not None else ""))
        self._ready = out
        return out

    async def report(self, mint: str, symbol: str = "", creator: str | None = None, creator_funder: str | None = None,
                     insiders: set | None = None, price_sol: float | None = None) -> dict:
        """What the Whale checker tells the Analyst about a token it is scoring:
        {"ready": False, "why"} when the data is not sufficient (the Analyst ignores it), else
        {"ready": True, "holders": n operators, "wallets": [...], "skipped": {...}}. Holders are read on-chain now
        (cached 60 s) and counted with the safeguards in the module docstring."""
        r = self.readiness()
        if not r["ready"]:
            return {"ready": False, "why": r["why"]}
        t0 = time.time()
        try:
            held = await asyncio.wait_for(self.holders(mint), timeout=float(self.c.analyst_timeout_seconds))
        except Exception as e:  # noqa: BLE001 - RPC slow / down: the Analyst scores without it
            self._reps.append({"t": time.time(), "secs": time.time() - t0, "timeout": True, "counted": 0,
                               "entries_tried": 0, "entries_ok": 0})
            return {"ready": False, "why": f"holders not read in time ({e.__class__.__name__})"}
        self.stats["reports"] += 1
        inside = {w for w in (insiders or set()) if w} | {w for w in (creator, creator_funder) if w}
        skipped = {"self_proof": 0, "insider": 0, "selling": 0, "same_operator": 0}
        counted: dict[str, tuple[str, dict]] = {}          # operator -> (wallet, record)
        for w, amt in held.items():
            rec = self.smart_for(w, mint)
            if rec is None:
                skipped["self_proof"] += 1
                continue
            op = self.operator(w)
            fund = (self.store.wallet_funder(w) or {}).get("funder")
            if w in inside or op in inside or (creator and fund == creator):
                skipped["insider"] += 1
                continue
            if self.selling(mint, w, amt):
                skipped["selling"] += 1
                continue
            if op in counted:
                skipped["same_operator"] += 1
                if rec["hit_rate"] > counted[op][1]["hit_rate"]:
                    counted[op] = (w, rec)
                continue
            counted[op] = (w, rec)
        valid = {w: held[w] for w, _ in counted.values()}
        self.record(mint, symbol, valid, "analyst", quiet=True)
        top = sorted(counted.values(), key=lambda x: -x[1]["hit_rate"])
        # Their own buy price: the Executor buys only near it (desk/whale_entry.py). SOL-weighted average.
        entries: dict[str, float] = {}
        if top:
            async def read_all():
                for w, _ in top[:5]:
                    e = await self.entry_price(mint, w)
                    if e:
                        entries[w] = e
            try:
                await asyncio.wait_for(read_all(), timeout=float(self.c.entry_lookup_seconds))
            except Exception:  # noqa: BLE001 - slow RPC: whatever was read is used
                pass
        entry = None
        if entries:
            amounts = {w: max(valid.get(w, 0.0), self._max_amt.get((mint, w), 0.0)) for w in entries}
            tot = sum(amounts.values())
            entry = (sum(entries[w] * amounts[w] for w in entries) / tot) if tot > 0 else \
                sum(entries.values()) / len(entries)
        prem = (price_sol / entry - 1) * 100 if entry and price_sol else None
        out = {"ready": True, "holders": len(counted), "wallets": [w[:6] for w, _ in top[:5]],
               "best_hit_rate": top[0][1]["hit_rate"] if top else None, "seen": len(held),
               "skipped": {k: v for k, v in skipped.items() if v}, "why": r["why"],
               "entry_sol": entry, "premium_pct": round(prem, 1) if prem is not None else None}
        self.reports[mint] = {"ts": time.time(), "wallets": [w for w, _ in top], "entry_sol": entry,
                              "entries": entries}
        self._reps.append({"t": time.time(), "secs": time.time() - t0, "timeout": False, "counted": len(counted),
                           "entries_tried": min(5, len(top)), "entries_ok": len(entries)})
        if len(self.reports) > 2000:
            self.reports = dict(list(self.reports.items())[-1000:])
        if held:
            best = top[0][1] if top else {}
            sk = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in out["skipped"].items())
            self.emit("info", f"-> Analyst: {len(counted)} smart wallet(s) count for {symbol or mint[:6]}"
                              + (f" (best {best.get('wins', 0)}/{best.get('tokens', 0)} early buys reached "
                                 f"${float(self.c.win_mcap_usd) / 1000:.0f}k)" if top else "")
                              + (f"; their buy price {entry:.3g} SOL, now {prem:+.0f}%" if prem is not None else "")
                              + (f"; not counted: {sk}" if sk else ""), mint, symbol, wallets=out["wallets"])
        return out

    # ── how well the harvest works (the Controller reads this) ──
    def efficiency(self, window_h: float = 6.0, now: float | None = None) -> dict:
        """Throughput and cost of the harvest over the last window_h hours (in-memory, cheap)."""
        now = now or time.time()
        since = now - window_h * 3600
        sc = [x for x in self._scans if x["t"] >= since]
        ok = [x for x in sc if x.get("complete")]
        long_ = [x for x in sc if not x.get("complete") and not x.get("error")]
        err = [x for x in sc if x.get("error")]
        rp = [x for x in self._reps if x["t"] >= since]
        span_h = max(1 / 60, min(window_h, (now - max(self.started, since)) / 3600))
        tried = sum(x["entries_tried"] for x in rp)
        avg = lambda xs: sum(xs) / len(xs) if xs else None      # noqa: E731
        return {
            "window_h": round(span_h, 2), "scans": len(sc), "complete": len(ok), "too_long": len(long_),
            "errors": len(err), "tokens_per_hour": round(len(sc) / span_h, 1),
            "secs_per_token": avg([x["secs"] for x in ok]), "calls_per_token": avg([x["calls"] for x in ok]),
            "pages_per_token": avg([x["pages"] for x in ok]),
            "calls_wasted_too_long": sum(x["calls"] for x in long_),
            "calls_total": sum(x["calls"] for x in sc), "too_long_share": len(long_) / len(sc) if sc else None,
            "usable_per_token": avg([x.get("usable", 0) for x in ok]), "dust": sum(x.get("dust", 0) for x in ok),
            "tx_per_usable": (sum(x.get("reads", 0) for x in ok) / max(1, sum(x.get("usable", 0) for x in ok)))
            if ok else None,
            "calls_per_min": round(self.calls / max(1.0, (now - self.started) / 60), 1),
            "lane_per_min": round(60 / max(0.01, float(self.c.rpc_interval_seconds)), 1),
            "backlog": self.backlog, "backfill_left": len(self._backfill),
            "reports": len(rp), "report_secs": avg([x["secs"] for x in rp if not x["timeout"]]),
            "report_timeouts": sum(1 for x in rp if x["timeout"]),
            "entries_tried": tried, "entries_ok": sum(x["entries_ok"] for x in rp),
            "smart": len(self.smart), "smart_before_balance": len(self.all_smart),
        }

    # ── background loop ──
    def _backfill_list(self) -> tuple[dict, list[str]]:
        """(bot peaks, past tokens to read: the winners first, then the rest, newest first). Worker thread."""
        peaks = self.bot_peaks(self._ro)
        mints = [r["mint"] for r in self._ro("SELECT mint, MAX(start_ts) AS t FROM shadow WHERE mint LIKE '%pump' "
                                             "GROUP BY mint ORDER BY t DESC")]
        win = float(self.c.win_mcap_usd)
        winners = [m for m in mints if peaks.get(m, (0, 0))[0] >= win]
        ws = set(winners)
        return peaks, winners + [m for m in mints if m not in ws]

    async def backfill_next(self) -> tuple[str, int] | None:
        """Next past token to read (the list is rebuilt at most once an hour)."""
        if not self._backfill and time.time() - self._backfill_ts >= 3600:
            self._backfill_ts = time.time()
            self._bot_peaks, todo = await asyncio.to_thread(self._backfill_list)
            self._backfill = [m for m in todo if m not in self._scanned]
            self._backfill.reverse()                        # pop() from the end
        while self._backfill:
            m = self._backfill.pop()
            if not self.scanned(m):
                win = self._bot_peaks.get(m, (0, 0))[0] >= float(self.c.win_mcap_usd)
                return m, int(self.c.backfill_winner_pages if win else self.c.max_sig_pages)
        return None

    async def _scan_safe(self, mint: str, symbol: str, source: str, pages: int | None = None) -> None:
        """scan(), and a token that keeps failing (3 times) is stored as unreadable instead of retried forever."""
        try:
            await self.scan(mint, symbol, source, pages)
            self._fails.pop(mint, None)
        except Exception:
            self._scans.append({"t": time.time(), "secs": 0.0, "calls": 0, "pages": 0, "complete": False,
                                "trades": 0, "source": source, "error": True})
            self._fails[mint] = self._fails.get(mint, 0) + 1
            if self._fails[mint] >= 3:
                self._fails.pop(mint, None)
                self._save_token({"mint": mint, "symbol": symbol, "complete": 0, "source": f"{source}:error"})
            raise

    async def loop(self, followed, watched, stop: asyncio.Event) -> None:
        """followed() -> [(mint, symbol)] to scan once; watched() -> [(mint, symbol, stage)] to watch."""
        c = self.c
        last_peaks = last_score = 0.0
        last_watch: dict[str, float] = {}
        while not stop.is_set():
            t = time.time()
            try:
                if t - last_score >= float(c.rescore_minutes) * 60:
                    last_score = t
                    n0 = len(self.smart)
                    await self.rescore_async()
                    await self.refresh_balances()
                    if len(self.smart) != n0:
                        self.emit("info", f"smart wallets: {len(self.smart)} (>= {c.min_wins} early buys that reached "
                                          f"${float(c.win_mcap_usd) / 1000:.0f}k, hit rate >= "
                                          f"{float(c.min_hit_rate) * 100:.0f}%, balance >= "
                                          f"${float(c.min_balance_usd):,.0f}; {len(self.all_smart)} before the "
                                          f"balance check)")
                if t - last_peaks >= float(c.peak_check_minutes) * 60:
                    last_peaks = t
                    await self.track_peaks()
                for mint, symbol, stage in watched():
                    every = float(c.watch_seconds if stage == "position" else c.watch_analyst_seconds)
                    if self.smart and time.time() - last_watch.get(mint, 0) >= every:
                        last_watch[mint] = time.time()
                        await self.watch(mint, symbol, stage)
                if self.smart:
                    await self.lookup_funders(1)
                todo_all = [(m, s) for m, s in followed() if m.endswith("pump") and not self.scanned(m)]
                self.backlog = len(todo_all)
                todo = todo_all[0] if todo_all else None
                if todo:
                    await self._scan_safe(todo[0], todo[1], "follow")
                    continue
                if c.backfill:
                    nxt = await self.backfill_next()
                    if nxt:
                        await self._scan_safe(nxt[0], "", "backfill", nxt[1])
                        self.stats["backfilled"] += 1
                        continue
            except Exception:  # noqa: BLE001 - public RPC busy: wait and go on
                self.stats["errors"] += 1
                await asyncio.sleep(20)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
