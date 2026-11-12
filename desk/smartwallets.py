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
2. WINNERS. A token "won" when its market cap reached `win_mcap_usd` ($200k). Peaks come from the desk's own
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
   winners and smart wallets) does the Analyst use it, as a bonus on its score; otherwise it ignores it.
   Nothing here buys or sells by itself: the Analyst's threshold, its caps and the Executor's checks still decide.
"""
from __future__ import annotations

import asyncio
import re
import time

from .devwatch import SPL_TOKEN, ata

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
        self.smart: dict[str, dict] = {r["wallet"]: r for r in store.q("SELECT * FROM sw_smart")}
        self._backfill: list[str] = []
        self._backfill_ts = 0.0
        self._bot_peaks: dict[str, tuple[float, float]] = {}
        self._fails: dict[str, int] = {}
        self._ready: dict = {}
        self.stats = {"scanned": 0, "too_long": 0, "trades": 0, "errors": 0, "hits": 0, "backfilled": 0,
                      "reports": 0}

    # ── RPC, paced: the public node is shared with the Filter, DevWatch and the funder backfill ──
    async def call(self, method: str, params: list):
        wait = self._last_call + float(self.c.rpc_interval_seconds) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.time()
        return await self.rpc.call(method, params, lane="smart")

    # ── 1. early buyers ──
    def scanned(self, mint: str) -> bool:
        return bool(self.store.q("SELECT 1 FROM sw_tokens WHERE mint=?", (mint,)))

    async def scan(self, mint: str, symbol: str = "", source: str = "follow", max_pages: int | None = None) -> dict:
        """Read the create transaction and the first trades of `mint` from the chain and store them."""
        pages = int(max_pages or self.c.max_sig_pages)
        before, oldest, complete = None, [], False
        for _ in range(max(1, pages)):
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
        # The creator's bundle (same slot as the create) is stored but does not use up the scan_trades budget:
        # on 2026-11-12 tokens it filled the first 10-15 trades. At most 2 x scan_trades transactions are read.
        rows, idx, reads = [], 0, 0
        for s in sigs[1:]:
            if idx >= int(self.c.scan_trades) or reads >= 2 * int(self.c.scan_trades):
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
            for t in trades:
                rows.append((mint, t["wallet"], s["signature"], s.get("slot"), s.get("blockTime"), idx, t["side"],
                             t["tokens"], t["sol"], int(t["signer"]), bundle, int(t["wallet"] == row["creator"])))
            if not bundle:
                idx += 1
        self.store.db.executemany("INSERT OR IGNORE INTO sw_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        row["n_trades"] = idx
        self._save_token(row)
        self.stats["scanned"] += 1
        self.stats["trades"] += idx
        return row

    def _save_token(self, r: dict) -> None:
        peak, pts = self.bot_peak(r["mint"])
        won = int(peak >= float(self.c.win_mcap_usd))
        self.store.db.execute(
            "INSERT OR REPLACE INTO sw_tokens(mint,symbol,creator,create_ts,create_slot,scanned_ts,complete,n_trades,"
            "peak_mcap,peak_ts,peak_checked,won,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["mint"], r.get("symbol") or "", r.get("creator"), r.get("create_ts"), r.get("create_slot"),
             time.time(), r["complete"], r.get("n_trades") or 0, peak, pts, 0.0, won, r.get("source") or ""))

    # ── 2. winners ──
    def bot_peaks(self) -> dict[str, tuple[float, float]]:
        """Highest real market cap the Filter ever read for each pump mint: mint -> (mcap, ts)."""
        out: dict[str, tuple[float, float]] = {}
        for r in self.store.q("SELECT ts, mint, data FROM decisions WHERE mint LIKE '%pump' "
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
        rows = self.store.q("SELECT mint, peak_mcap FROM sw_tokens WHERE won=0 AND complete=1 AND create_ts > ? "
                            "AND peak_checked < ? LIMIT 300",
                            (t - float(self.c.peak_track_hours) * 3600, t - float(self.c.peak_check_minutes) * 60))
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
    def rescore(self, now: float | None = None) -> dict[str, dict]:
        now = now or time.time()
        c = self.c
        decided_before = now - float(c.decide_hours) * 3600
        rows = self.store.q(
            "SELECT b.wallet, COUNT(*) AS tokens, SUM(t.won) AS wins, SUM(b.sol) AS sol_in, MAX(b.ts) AS last_ts "
            "FROM (SELECT mint, wallet, SUM(sol) AS sol, MIN(ts) AS ts FROM sw_trades "
            "      WHERE side='buy' AND signer=1 AND bundle=0 AND creator=0 GROUP BY mint, wallet) b "
            "JOIN sw_tokens t ON t.mint = b.mint "
            "WHERE t.complete=1 AND (t.won=1 OR t.create_ts < ?) GROUP BY b.wallet HAVING wins >= ? AND tokens >= ?",
            (decided_before, int(c.min_wins), int(c.min_tokens)))
        smart = {}
        for r in rows:
            hr = (r["wins"] or 0) / max(1, r["tokens"])
            if hr >= float(c.min_hit_rate):
                smart[r["wallet"]] = {**r, "hit_rate": round(hr, 3), "ts": now}
        ranked = sorted(smart.values(), key=lambda r: (-r["hit_rate"], -r["wins"]))[: int(c.max_smart_wallets)]
        self.store.db.execute("DELETE FROM sw_smart")       # derived list: rebuilt from sw_trades every time
        self.store.db.executemany("INSERT INTO sw_smart(wallet,tokens,wins,hit_rate,sol_in,last_ts,ts) "
                                  "VALUES(?,?,?,?,?,?,?)",
                                  [(r["wallet"], r["tokens"], r["wins"], r["hit_rate"], r["sol_in"] or 0.0,
                                    r["last_ts"], now) for r in ranked])
        self.smart = {r["wallet"]: r for r in ranked}
        return self.smart

    # ── 4. watch ──
    async def token_program(self, mint: str) -> str:
        if mint not in self._programs:
            info = await self.call("getAccountInfo", [mint, {"encoding": "base64"}])
            self._programs[mint] = ((info or {}).get("value") or {}).get("owner") or SPL_TOKEN
        return self._programs[mint]

    async def holders(self, mint: str) -> dict[str, float]:
        """Smart wallets holding `mint` right now -> UI amount (one RPC call per 100 wallets)."""
        if not self.smart:
            return {}
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
        return out

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
        return self.record(mint, symbol, await self.holders(mint), stage)

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
        out["ready"] = not missing
        out["why"] = ("not enough data yet: " + ", ".join(missing)) if missing else \
            f"{out['smart']} smart wallets from {out['decided']} tokens ({out['winners']} winners)"
        self._ready = out
        return out

    async def report(self, mint: str, symbol: str = "") -> dict:
        """What the Whale checker tells the Analyst about a token it is scoring:
        {"ready": False, "why"} when the data is not sufficient (the Analyst ignores it), else
        {"ready": True, "holders": n, "wallets": [...], "best_hit_rate"}. Holders are read on-chain now."""
        r = self.readiness()
        if not r["ready"]:
            return {"ready": False, "why": r["why"]}
        try:
            held = await asyncio.wait_for(self.holders(mint), timeout=float(self.c.analyst_timeout_seconds))
        except Exception as e:  # noqa: BLE001 - RPC slow / down: the Analyst scores without it
            return {"ready": False, "why": f"holders not read in time ({e.__class__.__name__})"}
        self.stats["reports"] += 1
        self.record(mint, symbol, held, "analyst", quiet=True)
        top = sorted(held, key=lambda w: -(self.smart.get(w) or {}).get("hit_rate", 0))
        out = {"ready": True, "holders": len(held), "wallets": [w[:6] for w in top[:5]],
               "best_hit_rate": (self.smart.get(top[0]) or {}).get("hit_rate") if top else None,
               "why": r["why"]}
        if held:
            best = self.smart.get(top[0]) or {}
            self.emit("info", f"-> Analyst: {len(held)} smart wallet(s) hold {symbol or mint[:6]} (best "
                              f"{best.get('wins', 0)}/{best.get('tokens', 0)} early buys reached "
                              f"${float(self.c.win_mcap_usd) / 1000:.0f}k)", mint, symbol, wallets=out["wallets"])
        return out

    # ── background loop ──
    def backfill_next(self) -> tuple[str, int] | None:
        """Next past token to read: the winners first (so the list fills quickly), then the rest, newest first."""
        if not self._backfill and time.time() - self._backfill_ts >= 3600:
            self._backfill_ts = time.time()
            self._bot_peaks = self.bot_peaks()
            done = {r["mint"] for r in self.store.q("SELECT mint FROM sw_tokens")}
            mints = [r["mint"] for r in self.store.q("SELECT mint, MAX(start_ts) AS t FROM shadow WHERE mint LIKE "
                                                     "'%pump' GROUP BY mint ORDER BY t DESC")]
            win = float(self.c.win_mcap_usd)
            winners = [m for m in mints if self._bot_peaks.get(m, (0, 0))[0] >= win]
            ws = set(winners)
            self._backfill = [m for m in winners + [m for m in mints if m not in ws] if m not in done]
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
        self.rescore()
        while not stop.is_set():
            t = time.time()
            try:
                if t - last_score >= float(c.rescore_minutes) * 60:
                    last_score = t
                    n0 = len(self.smart)
                    self.rescore()
                    if len(self.smart) != n0:
                        self.emit("info", f"smart wallets: {len(self.smart)} (>= {c.min_wins} early buys that reached "
                                          f"${float(c.win_mcap_usd) / 1000:.0f}k, hit rate >= "
                                          f"{float(c.min_hit_rate) * 100:.0f}%)")
                if t - last_peaks >= float(c.peak_check_minutes) * 60:
                    last_peaks = t
                    await self.track_peaks()
                for mint, symbol, stage in watched():
                    if self.smart and time.time() - last_watch.get(mint, 0) >= float(c.watch_seconds):
                        last_watch[mint] = time.time()
                        await self.watch(mint, symbol, stage)
                todo = next(((m, s) for m, s in followed() if m.endswith("pump") and not self.scanned(m)), None)
                if todo:
                    await self._scan_safe(todo[0], todo[1], "follow")
                    continue
                if c.backfill:
                    nxt = self.backfill_next()
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
