"""EXECUTOR — the only agent that touches money.

It will only buy a token when ALL of these hold:
  * the Verdict's Analyst signature verifies, and the embedded Filter clearance
    signature verifies, and that clearance shows every filter check passed;
  * the Analyst verdict is fresh (clearance_ttl_seconds), the Filter clearance too (plus the Analyst's
    own working time), and the score is >= 0.6;
  * Risk approves the entry (no averaging down, no revenge, cooldowns, limits);
  * the live buy quote's price impact and the buy->sell round-trip cost are acceptable
    (catches honeypots, hidden taxes and thin books right before execution);
  * QUANT: expected value after the measured round-trip cost is positive, using calibrated
    probabilities of +12% first / -6% first / neither within 60 min (blocks only, never forces).
Entry timing ([entry], desk/entry.py): with a mode other than "now" an approved token is not bought at once.
The Executor watches its price for up to [entry] window_minutes and buys at the moment the rule finds (a dip,
a bounce off a dip, a confirmation), always with a FRESH Analyst verdict and all the checks above; if the
moment doesn't come, or liquidity leaves while waiting, the token is dropped.
WHALE ENTRY (user 2026-11-12, desk/whale_entry.py): a token approved while smart wallets hold it (Whale checker,
30% of the score) is bought only within +30% of the smart wallets' own buy price and not on a 5-minute spike;
otherwise the Executor waits (up to [smart_wallets] whale_wait_minutes) for the price to come back. If one of those
smart wallets sells meanwhile, the token is dropped for whale_sold_block_hours: in that time it is bought only if
OTHER smart wallets (not the ones that sold) hold it, again near their own buy price. Same checks as above.
Size = max_position_pct (<= 8%, hard ceiling) of the CURRENT balance. Never more.
Exits run on their own loop and never wait for an entry (no shared swap lock).
"""
from __future__ import annotations

import asyncio
import math
import time

from ..clients.jupiter import SOL_MINT
from ..config import HARD_MAX_POSITION_PCT, HARD_MIN_SCORE
from ..models import ExitOrder, FilterPass, Position, Verdict, now
from .. import rugnames
from ..pools import pin_labels, quote_ok, route_check, route_text
from ..entry import EntryPlan, plan_from, settings as entry_settings
from .. import whale_entry
from ..portfolio import AveragingDownError
from ..quant import barriers, label_outcome, outcome_row
from .analyst import ANALYSIS_MAX_SECONDS
from .base import Agent


REF_QUOTE_LAMPORTS = 10_000_000      # 0.01 SOL reference quote for the price-impact check


class SizingError(RuntimeError):
    pass


def position_size(cash_sol: float, pct: float) -> float:
    """Size for one trade. Mathematically incapable of exceeding 8% of the balance."""
    pct = min(pct, HARD_MAX_POSITION_PCT)
    size = math.floor(cash_sol * pct / 100 * 1e9) / 1e9
    if size > cash_sol * HARD_MAX_POSITION_PCT / 100 + 1e-12:
        raise SizingError("size exceeds 8% hard cap")
    return max(0.0, size)


class Executor(Agent):
    name = "Executor"

    def __init__(self, desk, broker, risk) -> None:
        super().__init__(desk)
        self.broker = broker
        self.risk = risk
        self.e = self.cfg.executor
        self.plans: dict[str, dict] = {}         # mint -> approved token waiting for its entry moment
        self.timing_done: dict[str, float] = {}  # mint -> when its plan ended without a buy (no new plan for 1 h)
        self.timing_notes: dict[str, str] = {}   # mint -> why this moment was chosen (goes in the buy reason)
        self.whale_plans: dict[str, dict] = {}   # mint -> token approved thanks to smart wallets, waiting for a price
        self.whale_blocked: dict[str, dict] = {}   # mint -> {"ts", "sellers"}: a smart wallet sold while we waited
        # No swap lock: fills are measured per transaction (Jupiter's result / tx metadata), so an exit
        # never waits for an entry's swap to land.

    async def run(self) -> None:
        self.status("idle")
        self.emit("info", f"Executor online [{self.broker.mode.upper()}]: max {self.e.max_position_pct}% "
                          f"of balance per trade, max {self.e.max_open_positions} positions")
        await asyncio.gather(self.supervised("executor-exits", self.exit_loop),
                             self.supervised("executor-entries", self.loop),
                             self.supervised("executor-timing", self.timing_loop),
                             self.supervised("executor-whales", self.whale_loop))

    async def exit_loop(self) -> None:
        """Exits run on their own loop: a stop is never queued behind an entry's quotes and checks.
        Each exit gets its own task, so three stops hit at once are sold in parallel, not one by one."""
        running: set[asyncio.Task] = set()
        try:
            while not self.desk.stop.is_set():
                try:
                    order = await asyncio.wait_for(self.desk.exits.get(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                task = asyncio.create_task(self._safe_exit(order))
                running.add(task)
                task.add_done_callback(running.discard)
        finally:
            if running:
                await asyncio.gather(*running, return_exceptions=True)

    async def _safe_exit(self, order: ExitOrder) -> None:
        try:
            await self.handle_exit(order)
        except Exception as e:  # noqa: BLE001 - one broken exit must not kill the exit loop
            self.emit("error", f"exit crashed ({e.__class__.__name__}: {e}); Risk will re-issue it", order.mint)
            self.risk.exit_failed(order.mint)

    async def loop(self) -> None:
        while not self.desk.stop.is_set():
            try:
                v = await asyncio.wait_for(self.desk.verdicts.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if not self.desk.in_flight:
                    self.status("idle")
                continue
            await self.handle_verdict(v)

    # ── clearance verification ──
    def verify_clearance(self, v: Verdict) -> tuple[bool, str]:
        auth, fp = self.desk.authority, v.clearance
        t = now()
        if not isinstance(fp, FilterPass):
            return False, "missing Filter clearance"
        if not auth.verify("filter", FilterPass.payload(fp.report, fp.issued_at), fp.signature):
            return False, "Filter signature invalid"
        if not auth.verify("analyst", Verdict.payload(v.mint, v.score, v.trade, fp.signature, v.issued_at),
                           v.signature):
            return False, "Analyst signature invalid"
        if fp.report.mint != v.mint:
            return False, "clearance is for a different token"
        if not fp.report.passed or not fp.report.checks or not all(c.passed for c in fp.report.checks):
            return False, "not every filter check passed"
        if not v.trade or v.score < max(HARD_MIN_SCORE, self.cfg.analyst.min_score):
            return False, f"score {v.score:.2f} below threshold"
        ttl = self.e.clearance_ttl_seconds
        if t - v.issued_at > ttl:
            return False, f"Analyst clearance expired (> {ttl:.0f}s old)"
        # The Filter clearance also covers the Analyst's own working time (website check up to 90 s):
        # judging it by ttl alone expired good tokens whose website was slow to load.
        if t - fp.issued_at > ttl + ANALYSIS_MAX_SECONDS:
            return False, f"Filter clearance expired (> {ttl + ANALYSIS_MAX_SECONDS:.0f}s old)"
        return True, "ok"

    def reject(self, v: Verdict, reason: str) -> None:
        self.state.bump("declined")
        self.desk.store.add_decision(now(), "executor", "reject", v.mint, v.symbol, reason, {"score": v.score})
        self.emit("reject", f"no trade: {reason}", v.mint, v.symbol, deep=True)

    # ── entries ──
    async def handle_verdict(self, v: Verdict) -> None:
        ok, why = self.verify_clearance(v)
        if not ok:
            self.emit("error", f"REFUSED unverified order: {why}", v.mint, v.symbol)
            self.desk.store.add_decision(now(), "executor", "refuse", v.mint, v.symbol, why, {})
            return
        ok, why = self.risk.approve_entry(v.mint)
        if not ok:
            return self.reject(v, f"Risk: {why}")
        blk = self.whale_blocked.get(v.mint)
        exclude: set = set()
        if blk and now() - blk["ts"] < float(self.cfg.smart_wallets.whale_sold_block_hours) * 3600 \
                and v.mint not in self.whale_plans:
            # dropped because a smart wallet sold: bought again only if OTHER smart wallets hold it (user 2026-11-12)
            exclude = set(blk["sellers"])
            others = [w for w in self._smart_wallets(v.mint) if w not in exclude] if self.is_whale(v) else []
            if not others:
                return self.reject(v, "a smart wallet sold this token while we waited to buy it: dropped for "
                                      f"{self.cfg.smart_wallets.whale_sold_block_hours:g} h unless other smart "
                                      f"wallets buy it")
        if v.mint in self.whale_plans or self.is_whale(v):
            if not await self.whale_allows(v, exclude):
                return
        elif not await self.timing_allows(v):
            return
        self.desk.in_flight.add(v.mint)
        self.status("working")
        try:
            await self._enter(v)
        finally:
            self.desk.in_flight.discard(v.mint)
            self.timing_notes.pop(v.mint, None)

    # ── whale entry (desk/whale_entry.py) ──
    def is_whale(self, v: Verdict) -> bool:
        """Approved with smart wallets weighing in (Whale checker), and their buy price is known."""
        f = v.features or {}
        return bool(f.get("whale_free_score") is not None and (f.get("whale_holders") or 0) > 0
                    and f.get("whale_entry_sol") and getattr(self.desk, "smart", None) is not None)

    def _smart_wallets(self, mint: str) -> list[str]:
        """The smart wallets counted for this token in the Whale checker's last report to the Analyst."""
        sw = getattr(self.desk, "smart", None)
        return list(((sw.reports.get(mint) or {}) if sw is not None else {}).get("wallets") or [])

    def _price_sol(self, snap) -> float:
        if snap is None:
            return 0.0
        if snap.price_native > 0:
            return snap.price_native
        usd = self.desk.portfolio.sol_usd
        return snap.price_usd / usd if usd and snap.price_usd > 0 else 0.0

    async def whale_allows(self, v: Verdict, exclude: set | None = None) -> bool:
        """True = buy now. False = waiting for a price near the smart wallets' own (or dropped).
        exclude: smart wallets that sold this token before (their price and holdings don't count any more)."""
        p = self.whale_plans.get(v.mint)
        if p is not None:
            p["verdict"] = v
            if p["state"] == "buy":
                self.whale_plans.pop(v.mint, None)
                self.timing_notes[v.mint] = "wallet bravi: " + p["why"]
                return True
            return False
        done = self.timing_done.get(v.mint)
        if done and now() - done < self.cfg.quant.horizon_minutes * 60:
            return False                          # its price didn't come back: not re-planned for an hour
        sw = self.desk.smart
        rep = (sw.reports.get(v.mint) or {}) if sw is not None else {}
        ref = float((v.features or {}).get("whale_entry_sol") or rep.get("entry_sol") or 0)
        wallets = list(rep.get("wallets") or [])
        if exclude:
            wallets = [w for w in wallets if w not in exclude]
            entries = [e for w, e in (rep.get("entries") or {}).items() if w in wallets and e]
            ref = sum(entries) / len(entries) if entries else 0.0
            if not wallets or not ref:
                self.reject(v, "other smart wallets hold it, but their buy price can't be read: still dropped")
                return False
        if len(self.whale_plans) >= 20:
            self.reject(v, "too many smart-wallet tokens already waiting for their price")
            return False
        snap = await self.desk.dex.snapshot(v.mint)
        if snap is None or snap.price_usd <= 0:
            self.reject(v, "whale entry: no price to start from")
            return False
        t = now()
        p = {"t0": t, "ref_sol": ref, "wallets": wallets, "verdict": v, "symbol": v.symbol, "state": "wait",
             "why": "", "ref_liq": snap.liquidity_usd, "ref_pair": snap.pair_address, "armed_ts": 0.0,
             "sold_check": 0.0, "exclude": set(exclude or ())}
        self.whale_plans[v.mint] = p
        st = await self.whale_look(v.mint, p, snap, t)
        if st == "buy":
            self.whale_plans.pop(v.mint, None)
            self.timing_notes[v.mint] = "wallet bravi: " + p["why"] + (" (altri wallet bravi dopo una vendita)"
                                                                        if exclude else "")
            return True
        if st == "wait":
            msg = (f"approvato grazie ai wallet bravi{' (altri, dopo una vendita)' if exclude else ''}, "
                   f"aspetto il prezzo: {p['why']}")
            self.desk.store.add_decision(t, "executor", "wait", v.mint, v.symbol, msg,
                                         {"score": v.score, "whale_entry_sol": ref, "wallets": wallets})
            self.emit("info", msg, v.mint, v.symbol)
        return False

    async def whale_look(self, mint: str, p: dict, snap, t: float) -> str:
        """One look at a waiting whale token: price vs the smart wallets' buy price, spike, liquidity, sellers."""
        c = self.cfg.smart_wallets
        sold: list[str] = []
        if p["wallets"] and t - p["sold_check"] >= 30 and getattr(self.desk, "smart", None) is not None:
            p["sold_check"] = t
            try:
                sold = await self.desk.smart.sold(mint, p["wallets"])
            except Exception:  # noqa: BLE001 - RPC busy: checked again on the next look
                sold = []
        if snap is not None and p.get("ref_pair") and snap.pair_address and snap.pair_address != p["ref_pair"]:
            snap = None                                   # another pool's price: not comparable
        price = self._price_sol(snap)
        liq = snap.liquidity_usd / p["ref_liq"] if snap is not None and p["ref_liq"] > 0 else None
        st, why = whale_entry.decide(t - p["t0"], price, p["ref_sol"], snap.change_m5 if snap else 0.0, liq, sold, c,
                                     self.cfg.entry.max_liq_drop_pct)
        p["sold"] = sold
        p["why"] = why
        if st == "buy":
            p["state"], p["armed_ts"] = "buy", t
        elif st in ("skip", "cancel", "sold"):
            self.whale_plans.pop(mint, None)
            if st == "sold":
                prev = (self.whale_blocked.get(mint) or {}).get("sellers") or set()
                self.whale_blocked[mint] = {"ts": t, "sellers": set(prev) | set(p.get("exclude") or ())
                                            | set(p.get("sold") or ())}
            else:
                self.timing_done[mint] = t        # a sale has its own block (lifted by other smart wallets)
            self.state.bump("whale_drop")
            self.desk.store.add_decision(t, "executor", "reject", mint, p["symbol"], f"whale entry: {why}",
                                         {"score": p["verdict"].score})
            self.emit("reject", f"no trade: {why}", mint, p["symbol"], deep=True)
        return st

    async def whale_loop(self) -> None:
        """Waiting whale tokens: a look every 12 s; when the price is right, the freshest verdict
        goes back through handle_verdict (all checks again). An armed plan waits 150 s for a fresh verdict."""
        while not self.desk.stop.is_set():
            # every 12 s: the "entry" DexScreener lane (10/min) is shared with the normal entry plans
            if not await self.sleep(max(12.0, 2 * float(self.cfg.entry.poll_seconds))):
                return
            if not self.whale_plans:
                continue
            try:
                snaps = await self.desk.dex.snapshots(list(self.whale_plans), fresh=True, lane="entry")
            except Exception:  # noqa: BLE001 - next tick retries
                continue
            t = now()
            for mint, p in list(self.whale_plans.items()):
                if p["state"] == "buy":
                    if t - p["armed_ts"] > 150:
                        self.whale_plans.pop(mint, None)
                        self.desk.store.add_decision(t, "executor", "reject", mint, p["symbol"],
                                                     f"whale entry: {p['why']}, ma nessun segnale fresco dell'Analyst "
                                                     f"entro 150 s", {"score": p["verdict"].score})
                    elif self.verify_clearance(p["verdict"])[0]:
                        await self.desk.verdicts.put(p["verdict"])
                    continue
                if await self.whale_look(mint, p, (snaps or {}).get(mint), t) == "buy":
                    self.emit("info", f"prezzo giusto per i wallet bravi: {p['why']}", mint, p["symbol"])
                    if self.verify_clearance(p["verdict"])[0]:
                        await self.desk.verdicts.put(p["verdict"])

    # ── entry timing ──
    def rule_text(self) -> str:
        en = self.cfg.entry
        return {"dip": f"ribasso di {en.dip_pct:g}% entro {en.window_minutes:g} min",
                "bounce": f"ribasso di {en.dip_pct:g}% poi rimbalzo di {en.bounce_pct:g}% entro {en.window_minutes:g} min",
                "confirm": f"prezzo >= {en.confirm_pct:+g}% dopo {en.confirm_minutes:g} min"}.get(en.mode, "subito") + \
            (" (altrimenti compra a fine finestra)" if en.mode in ("dip", "bounce") and en.fallback == "buy" else "")

    async def timing_allows(self, v: Verdict) -> bool:
        """True = buy now. False = the token waits for its moment (or its plan already ended)."""
        p = self.plans.get(v.mint)
        if p is not None:
            p["verdict"] = v                      # freshest clearance, used when the moment comes
            if p["plan"].state == "buy":
                self.plans.pop(v.mint, None)
                self.timing_notes[v.mint] = p["plan"].why
                return True
            return False
        if self.cfg.entry.mode == "now":
            return True
        t = now()
        done = self.timing_done.get(v.mint)
        if done and t - done < self.cfg.quant.horizon_minutes * 60:
            return False                          # its moment didn't come: not re-planned for an hour (as measured)
        if len(self.plans) >= 40:
            self.reject(v, "too many approved tokens already waiting for their entry moment")
            return False
        snap = await self.desk.dex.snapshot(v.mint)
        if snap is None or snap.price_usd <= 0:
            self.reject(v, "entry timing: no price to start from")
            return False
        plan = plan_from(entry_settings(self.cfg.entry))
        plan.max_liq_drop_pct = self.cfg.entry.max_liq_drop_pct
        # ref_pair: every later price must come from this same pool. BERNIE (2026-11-11) was bought on a fake
        # "-51.5% dip": the reference came from a dust SOL pool, the next tick from the real SPCX pool.
        self.plans[v.mint] = {"plan": plan, "t0": t, "ref_price": snap.price_usd, "ref_liq": snap.liquidity_usd,
                              "ref_pair": snap.pair_address, "verdict": v, "symbol": v.symbol, "armed_ts": 0.0}
        self.state.bump("timing_wait")
        msg = f"approvato, aspetto il momento giusto: {self.rule_text()}"
        self.desk.store.add_decision(t, "executor", "wait", v.mint, v.symbol, msg, {"score": v.score,
                                     "ref_price": snap.price_usd, "rule": entry_settings(self.cfg.entry)})
        self.emit("info", msg, v.mint, v.symbol)
        return False

    def _end_plan(self, mint: str, p: dict, why: str) -> None:
        self.plans.pop(mint, None)
        self.timing_done[mint] = now()
        if len(self.timing_done) > 5000:
            cut = now() - 3600
            self.timing_done = {k: x for k, x in self.timing_done.items() if x > cut}
        self.state.bump("timing_skip")
        self.desk.store.add_decision(now(), "executor", "reject", mint, p["symbol"], f"entry timing: {why}",
                                     {"score": p["verdict"].score})
        self.emit("reject", f"no trade: entry timing: {why}", mint, p["symbol"], deep=True)

    def timing_step(self, snaps: dict, t: float) -> list[Verdict]:
        """Advance every waiting plan with fresh prices (pure apart from bookkeeping). Returns the verdicts to buy
        now: a plan whose moment came and whose last Analyst verdict is still fresh. A plan whose moment came with
        a stale verdict stays armed and buys on the Analyst's next verdict (re-scored every ~90 s), for 150 s."""
        go = []
        for mint, p in list(self.plans.items()):
            plan: EntryPlan = p["plan"]
            if plan.state == "buy":
                if t - p["armed_ts"] > 150:
                    self._end_plan(mint, p, f"{plan.why}, ma nessun segnale fresco dell'Analyst entro 150 s")
                continue
            snap = snaps.get(mint)
            if snap is not None and p.get("ref_pair") and snap.pair_address and snap.pair_address != p["ref_pair"]:
                snap = None                       # another pool's price: not comparable with the reference
            if snap is None or snap.price_usd <= 0:
                if t - p["t0"] > plan.window_s + 60:
                    self._end_plan(mint, p, "nessun prezzo durante l'attesa")
                continue
            move = (snap.price_usd / p["ref_price"] - 1) * 100
            liq = snap.liquidity_usd / p["ref_liq"] if p["ref_liq"] > 0 else None
            st = plan.update(t - p["t0"], move, liq)
            if st == "buy":
                p["armed_ts"] = t
                self.emit("info", f"momento d'acquisto: {plan.why}", mint, p["symbol"])
                ok, _ = self.verify_clearance(p["verdict"])
                if ok:
                    go.append(p["verdict"])
            elif st in ("skip", "cancel"):
                self._end_plan(mint, p, plan.why)
        return go

    async def timing_loop(self) -> None:
        while not self.desk.stop.is_set():
            if not await self.sleep(max(3.0, float(self.cfg.entry.poll_seconds))):
                return
            if not self.plans:
                continue
            try:
                snaps = await self.desk.dex.snapshots(list(self.plans), fresh=True, lane="entry")
            except Exception:  # noqa: BLE001 - next tick retries
                continue
            snaps = dict(snaps or {})
            # The batched feed returns ONE pair per token, not always the observed one: re-read those on the full
            # pair list (whose main pair is chosen the same way as the reference's).
            for mint, p in list(self.plans.items()):
                s = snaps.get(mint)
                if p.get("ref_pair") and (s is None or s.pair_address != p["ref_pair"]):
                    try:
                        full = await self.desk.dex.snapshot(mint, lane="entry")
                    except Exception:  # noqa: BLE001
                        full = None
                    if full is not None:
                        snaps[mint] = full
            for v in self.timing_step(snaps or {}, now()):
                await self.desk.verdicts.put(v)  # back through handle_verdict: clearance + Risk checks again

    async def _enter(self, v: Verdict) -> None:
        pf = self.desk.portfolio
        t_start = time.monotonic()

        async def sized_quote():
            cash = await self.broker.cash()
            if not pf.positions and self.broker.mode == "live":
                pf.set_cash(cash)
            size = position_size(cash, self.e.max_position_pct)
            if size < self.e.min_trade_sol:
                return cash, size, None
            # Live (jupiter_v2): this is the executable order itself (quote + transaction), so what passes
            # the checks below is exactly what gets sent. Paper: a plain Jupiter quote.
            return cash, size, await self.broker.quote_buy(self.desk.jup, v.mint, int(size * 1e9),
                                                           self.e.slippage_bps)

        async def token_decimals():
            d = v.clearance.report.rug.get("decimals")
            if d is None:
                info = await self.desk.rpc.mint_info(v.mint)
                d = (info or {}).get("decimals") or 6
            return int(d)

        async def reference_quote():
            # A tiny quote taken at the same moment = the pool's marginal price right now. Price impact is
            # measured against it, not against DexScreener's price, which lags on a moving memecoin (seen
            # live: real impact 3.1%, measured 11% against the DexScreener price).
            try:
                return await self.desk.jup.quote(SOL_MINT, v.mint, REF_QUOTE_LAMPORTS, self.e.slippage_bps)
            except Exception:  # noqa: BLE001 - optional: fall back to the DexScreener price
                return None

        # Balance -> size -> buy quote, token decimals and fresh market data are independent: fetch together.
        got = await asyncio.gather(sized_quote(), token_decimals(), self.desk.dex.snapshot(v.mint),
                                   reference_quote(), return_exceptions=True)
        for g in got:
            if isinstance(g, BaseException):
                return self.reject(v, f"pre-trade data failed: {g.__class__.__name__}: {g}")
        (cash, size, buy_q), decimals, snap, ref_q = got
        if size < self.e.min_trade_sol:
            return self.reject(v, f"size {size:.4f} SOL below minimum {self.e.min_trade_sol} (balance {cash:.3f})")
        if snap is None:
            return self.reject(v, "market data vanished before execution")
        if not buy_q:
            return self.reject(v, "no Jupiter buy route")
        # The pool: buy in the one the Filter/Analyst observed, in the currency it trades against (desk/pools.py).
        pool_note = ""
        observed = v.clearance.report.market
        target = (observed.pair_address if observed is not None and observed.pair_address else "") or snap.pair_address
        allowed = list(self.cfg.filter.allowed_quote_mints)
        tq = ((snap.pool(target) or {}).get("quote_mint") if snap.pools and target else "") or snap.quote_mint
        if not quote_ok(tq, allowed):
            return self.reject(v, f"pool trades against {(snap.pool(target) or {}).get('quote_symbol') or tq[:6]}, "
                                  f"not SOL: the desk buys only in SOL pools")
        if snap.pools and target:
            pool = snap.pool(target)
            if pool is None:
                return self.reject(v, f"observed pool {target[:6]}… is no longer listed for this token")
            seen = (observed.pool(target) if observed is not None else None) or {}
            seen_liq = float(seen.get("liquidity_usd") or (observed.pair_liquidity_usd if observed else 0) or 0)
            liq = float(pool.get("liquidity_usd") or 0)
            if liq < self.cfg.filter.min_liquidity_usd:
                return self.reject(v, f"observed pool {pool.get('dex')} {target[:6]}… has only ${liq:,.0f} liquidity "
                                      f"(< ${self.cfg.filter.min_liquidity_usd:,.0f})")
            drop = self.cfg.entry.max_liq_drop_pct
            if seen_liq > 0 and liq < seen_liq * (1 - drop / 100):
                return self.reject(v, f"observed pool liquidity fell from ${seen_liq:,.0f} to ${liq:,.0f} "
                                      f"(> {drop:g}% drop) since the analysis")
            ok, why = route_check(buy_q, v.mint, target)
            if not ok:
                labels = pin_labels(pool, snap.pools)
                if not labels:
                    return self.reject(v, f"{why}; DEX '{pool.get('dex')}' unknown, can't force the observed pool")
                try:
                    pinned = await self.broker.quote_buy_pinned(self.desk.jup, v.mint, int(size * 1e9),
                                                                self.e.slippage_bps, labels)
                except Exception as e:  # noqa: BLE001
                    pinned, e_txt = None, f"{e.__class__.__name__}: {e}"
                else:
                    e_txt = "no route"
                ok2, why2 = route_check(pinned, v.mint, target) if pinned else (False, e_txt)
                if not ok2:
                    return self.reject(v, f"{why}; restricted to {', '.join(labels)}: {why2}")
                buy_q, pool_note = pinned, " (route forced on the observed pool)"
                ref_q = None
                try:
                    ref_q = await self.desk.jup.quote(SOL_MINT, v.mint, REF_QUOTE_LAMPORTS, self.e.slippage_bps,
                                                      dexes=labels)
                except Exception:  # noqa: BLE001 - optional
                    pass
            if ref_q is not None and not route_check(ref_q, v.mint, target)[0]:
                ref_q = None                       # a reference price from another pool would skew the impact
            sym = {v.mint: v.symbol, pool.get("quote_mint") or "": pool.get("quote_symbol") or "?"}
            pool_note = (f"pool {pool.get('dex')} {target[:6]}… vs {pool.get('quote_symbol') or '?'} "
                         f"(${liq:,.0f}){pool_note}; route {route_text(buy_q, sym)}")
        est_bps = buy_q.get("slippageBps")
        if est_bps is not None and int(est_bps) > self.e.slippage_bps:
            return self.reject(v, f"Jupiter's slippage estimate {int(est_bps)} bps > cap {self.e.slippage_bps} bps "
                                  "(too volatile/thin to enter safely)")
        out_tokens = int(buy_q["outAmount"]) / 10 ** decimals
        ref_out = int((ref_q or {}).get("outAmount") or 0)
        if ref_out > 0 and size * 1e9 > 2 * REF_QUOTE_LAMPORTS:
            fair = ref_out / 10 ** decimals * (size * 1e9 / REF_QUOTE_LAMPORTS)
            impact = max(0.0, (1 - out_tokens / fair) * 100)
        elif snap.price_native > 0:
            fair = size / snap.price_native
            impact = max(0.0, (1 - out_tokens / fair) * 100)
        else:
            impact = float(buy_q.get("priceImpactPct") or 0) * 100
        if impact > self.e.max_entry_price_impact_pct:
            return self.reject(v, f"entry price impact {impact:.2f}% > {self.e.max_entry_price_impact_pct}%")
        try:
            sell_q = await self.desk.jup.quote(v.mint, SOL_MINT, int(buy_q["outAmount"]), self.e.slippage_bps)
        except Exception as e:  # noqa: BLE001 - record why, instead of crashing the entry loop silently
            return self.reject(v, f"sell-quote check failed: {e.__class__.__name__}: {e}")
        if not sell_q:
            return self.reject(v, "HONEYPOT GUARD: no sell route for the tokens we'd buy")
        back = int(sell_q["outAmount"]) / 1e9
        rt_loss = (1 - back / size) * 100
        if rt_loss > self.e.max_round_trip_loss_pct:
            return self.reject(v, f"round-trip cost {rt_loss:.2f}% > {self.e.max_round_trip_loss_pct}% "
                                  f"(tax/honeypot/thin book)")
        cost = max(0.0, rt_loss)
        quant = self.desk.quant
        quant.cost_samples.append(cost)
        quant.min_score = self.cfg.analyst.min_score     # follows Controller changes to the Analyst threshold
        qv = quant.evaluate(v.score, cost)
        if self.cfg.quant.enabled and not qv.passes:
            self.state.bump("ev_block")
            return self.reject(v, qv.reason)
        # MODELLO: the Controller's statistical model (desk/model.py). Blocks only once verified on unseen data.
        mv = None
        if self.desk.model is not None:
            regime = self.desk.market.snapshot() if self.desk.market is not None else {}
            mv = self.desk.model.evaluate({**regime, **v.features})
            if mv is not None and mv.block:
                self.state.bump("model_block")
                return self.reject(v, mv.reason)

        # Exits run concurrently: a loss may have landed while we were quoting. Ask Risk again right
        # before the swap (cooldown / streak pause / daily halt / max positions). No await in between.
        ok, why = self.risk.approve_entry(v.mint, own_order=True)
        if not ok:
            return self.reject(v, f"Risk (re-check before swap): {why}")
        t_send = time.monotonic()
        fill = await self.broker.buy(v.mint, size, decimals, buy_q)
        t_done = time.monotonic()
        if not fill.ok or fill.tokens <= 0:
            return self.reject(v, f"buy failed: {fill.error or 'no tokens received'} {fill.tx}")
        speed = {"checks_ms": round((t_send - t_start) * 1000), "swap_ms": round((t_done - t_send) * 1000),
                 "signal_to_fill_ms": round((now() - v.issued_at) * 1000), "route": fill.route}
        self.desk.exec_stats.append({"side": "buy", **speed})

        rep = v.clearance.report
        timing = self.timing_notes.pop(v.mint, "")
        reason = (f"score {v.score:.2f} | {'; '.join(v.rationale[:5])} |{' timing: ' + timing + ' |' if timing else ''} filters: "
                  + ", ".join(f"{c.name}={c.value}" for c in rep.checks)
                  + f" | size {size:.4f} SOL = {self.e.max_position_pct}% of {cash:.3f} | impact {impact:.2f}% "
                    f"| round-trip {rt_loss:.2f}% |{' ' + pool_note + ' |' if pool_note else ''} {qv.short()}{' | ' + mv.short() if mv else ''} | exec {fill.route}: checks "
                    f"{speed['checks_ms']} ms + swap {speed['swap_ms']} ms")
        pos = Position(
            mint=v.mint, symbol=v.symbol, tokens=fill.tokens, decimals=decimals, cost_sol=fill.sol,
            entry_price_sol=fill.sol / fill.tokens, entry_ts=now(), entry_liquidity_usd=snap.liquidity_usd,
            exit_cost_pct=max(0.0, rt_loss / 2), score=v.score, entry_reason=reason,
            mark_value_sol=fill.sol * (1 - max(0.0, rt_loss) / 100), tx_in=fill.tx,
            rt_cost_pct=cost, p_up=qv.p["up"], p_down=qv.p["down"], p_neither=qv.p["neither"], ev_pct=qv.ev_pct,
            regime=self.desk.market.snapshot() if self.desk.market is not None else {},
        )
        r = self.cfg.risk
        pos.exit_rules = {"stop_pct": r.exit_stop_pct or r.hard_stop_pct, "trail_activate_pct": r.trail_activate_pct,
                          "trail_distance_pct": r.trail_distance_pct, "max_hold_minutes": r.max_hold_minutes,
                          "profit_lock_activate_pct": r.profit_lock_activate_pct,
                          "profit_lock_fast_activate_pct": r.profit_lock_fast_activate_pct,
                          "profit_lock_fast_minutes": r.profit_lock_fast_minutes,
                          "profit_lock_pct": r.profit_lock_pct, "profit_lock_keep_pct": r.profit_lock_keep_pct,
                          "profit_lock_fee_multiple": r.profit_lock_fee_multiple,
                          "sell_buffer_pct": self.risk.sell_buffer() if hasattr(self.risk, "sell_buffer") else None}
        pos.peak_value_sol = pos.mark_value_sol
        pos.last_price_ts = now()
        pos.last_liquidity_usd = snap.liquidity_usd
        try:
            pf.open(pos)
        except AveragingDownError as e:
            return self.reject(v, str(e))
        await self._reconcile_cash(v.mint)
        self.state.bump("buys")
        if self.desk.shadow is not None:
            self.desk.shadow.mark_traded(v.mint)
        self.desk.store.add_decision(now(), "executor", "buy", v.mint, v.symbol, reason,
                                     {"breakdown": v.breakdown, "website": v.website.get("url"), "tx": fill.tx,
                                      "exec": speed})
        self.emit("trade", f"BUY {fill.sol:.4f} SOL -> {fill.tokens:,.0f} {v.symbol} (score {v.score:.2f}, "
                           f"impact {impact:.1f}%, rt {rt_loss:.1f}%, EV {qv.ev_pct:+.1f}%, "
                           f"{speed['checks_ms'] + speed['swap_ms']} ms)", v.mint, v.symbol, reason=reason, tx=fill.tx)

    async def _reconcile_cash(self, mint: str) -> None:
        """Live: sync cash with the wallet, but only when no other swap is in flight (its SOL would be
        counted twice: once in the wallet balance and again when its own fill is booked)."""
        if self.broker.mode == "live" and not (self.desk.in_flight - {mint}):
            self.desk.portfolio.set_cash(await self.broker.cash())

    # ── exits ──
    async def handle_exit(self, order: ExitOrder) -> None:
        pf = self.desk.portfolio
        pos = pf.positions.get(order.mint)
        if pos is None:
            self.risk.exit_done(order.mint)
            return
        self.status("working")
        self.desk.in_flight.add(order.mint)
        try:
            if order.fraction < 1:
                await self._scale_out(order, pos)
            else:
                await self._exit(order, pos)
        finally:
            self.desk.in_flight.discard(order.mint)

    async def _scale_out(self, order: ExitOrder, pos: Position) -> None:
        """Partial sale (user's rule 2026-11-12): sell `fraction` of the held tokens once; the rest stays open
        under the usual stops. If no attempt fills, Risk re-issues it on a later tick (the position is untouched)."""
        pf = self.desk.portfolio
        t0 = time.monotonic()
        tokens = pos.tokens * order.fraction
        err = ""
        for i, bps in enumerate([None] + list(self.e.exit_slippage_ladder_bps)):
            raw = int(tokens * 10 ** pos.decimals)
            if raw <= 0:
                err = "nothing to sell"
                break
            try:
                quote = await self.broker.quote_sell(self.desk.jup, order.mint, raw, bps)
            except Exception as e:  # noqa: BLE001
                quote, err = None, f"quote error: {e}"
            if not quote:
                err = err or "no sell route"
                continue
            fill = await self.broker.sell(order.mint, tokens, pos.decimals, quote)
            if fill.ok:
                if pf.positions.get(order.mint) is not pos:
                    break                        # closed meanwhile (cannot happen while it is in flight)
                cost_before = pos.cost_sol
                f = pf.scale_out(order.mint, fill.sol, fill.tokens or tokens)
                await self._reconcile_cash(order.mint)
                self.risk.scale_out_done(order.mint)
                part_cost = cost_before * f
                pnl = fill.sol - part_cost
                ms = round((time.monotonic() - t0) * 1000)
                self.desk.exec_stats.append({"side": "sell", "exit_ms": ms, "attempts": i + 1, "route": fill.route})
                self.desk.store.add_decision(now(), "executor", "scale_out", order.mint, pos.symbol,
                                             f"SCALE_OUT: {order.detail}",
                                             {"share": f, "proceeds_sol": fill.sol, "cost_sol": part_cost,
                                              "pnl_sol": pnl, "pnl_pct": (fill.sol / part_cost - 1) * 100
                                              if part_cost else 0.0})
                self.emit("trade", f"PARTIAL SELL {pos.symbol} {f * 100:.0f}%: {fill.sol:.4f} SOL "
                                   f"({pnl:+.4f} SOL on that part); {pos.tokens:,.0f} tokens still open",
                          order.mint, pos.symbol, pnl_sol=pnl)
                return
            err = fill.error or "sell failed"
        self.emit("warn", f"partial sale not done ({err}); Risk will retry", order.mint, pos.symbol)
        self.risk.exit_failed(order.mint)

    async def _exit(self, order: ExitOrder, pos: Position) -> None:
        """Get OUT, fast. First try: Jupiter's own slippage estimate (best price). If that fails, retry
        immediately (not on Risk's next tick) at each wider slippage of the ladder, re-reading the wallet."""
        pf = self.desk.portfolio
        t0 = time.monotonic()
        tokens = pos.tokens          # measured at entry: no wallet read before the first attempt
        attempts = [None] + list(self.e.exit_slippage_ladder_bps)
        quote, last_quote, err = None, None, ""
        for i, bps in enumerate(attempts):
            if i and self.broker.mode == "live":
                held = await self.broker.token_holdings(order.mint)
                if held is not None and held <= 0:
                    # A previous attempt landed after all (e.g. the response was lost): book it.
                    proceeds = int((last_quote or {}).get("otherAmountThreshold")
                                   or (last_quote or {}).get("outAmount") or 0) / 1e9
                    trade = pf.close(order.mint, proceeds, f"{order.reason}: {order.detail} "
                                                           f"(sold on an earlier attempt, proceeds estimated)")
                    await self._reconcile_cash(order.mint)
                    return self._finish(trade, order, pos, {"route": "unknown", "attempts": i})
                if held:
                    tokens = held
            raw = int(tokens * 10 ** pos.decimals)
            try:
                quote = await self.broker.quote_sell(self.desk.jup, order.mint, raw, bps)
            except Exception as e:  # noqa: BLE001
                quote, err = None, f"quote error: {e}"
            if not quote:
                err = err or "no sell route"
                continue
            last_quote = quote
            fill = await self.broker.sell(order.mint, tokens, pos.decimals, quote)
            if fill.ok:
                speed = {"exit_ms": round((time.monotonic() - t0) * 1000),
                         "trigger_to_fill_ms": round((now() - order.ts) * 1000), "attempts": i + 1,
                         "route": fill.route}
                self.desk.exec_stats.append({"side": "sell", **speed})
                trade = pf.close(order.mint, fill.sol, f"{order.reason}: {order.detail}", fill.tx, {"exec": speed})
                await self._reconcile_cash(order.mint)
                return self._finish(trade, order, pos, speed)
            err = fill.error or "sell failed"
            if i + 1 < len(attempts):
                self.emit("warn", f"sell attempt {i + 1} failed ({err}) -> retrying now at "
                                  f"{attempts[i + 1]} bps", order.mint, pos.symbol)
        if last_quote is None and order.reason in ("LIQUIDITY_PULL", "DATA_LOSS", "DEV_SOLD") and self.broker.mode == "paper":
            trade = pf.close(order.mint, 0.0, f"{order.reason}: no route, marked total loss")
            return self._finish(trade, order, pos)
        self.emit("error", f"EXIT BLOCKED ({order.reason}): {err}; Risk will re-issue it", order.mint, pos.symbol)
        self.risk.exit_failed(order.mint)

    def _finish(self, trade: dict, order: ExitOrder, pos: Position, speed: dict | None = None) -> None:
        self.risk.exit_done(order.mint)
        self.risk.record_result(trade)
        f = self.cfg.filter
        if trade["pnl_pct"] <= -f.rugged_name_loss_pct and trade["exit_ts"] - trade["entry_ts"] <= 3600:
            # Instant rug: the same hype name comes back on a new mint with a fresh dev wallet.
            rugnames.record(self.desk.store, trade["mint"], trade["symbol"], None,
                            f"our trade lost {trade['pnl_pct']:.0f}% ({order.reason})",
                            creator=self.desk.creators.get(trade["mint"]))
        # QUANT learning sample: which barrier did this trade touch first within the horizon?
        label = label_outcome(pos.entry_ts, trade["exit_ts"], pos.hit_up_ts, pos.hit_down_ts, trade["pnl_pct"],
                              self.cfg.quant.horizon_minutes * 60, barriers(self.cfg.risk)[1])
        row = outcome_row(trade, pos, label, self.cfg.mode)
        self.desk.store.add_outcome(row)
        self.desk.quant.add(row)
        self.state.bump("sells")
        self.state.bump("wins" if trade["pnl_sol"] > 0 else "losses")
        self.desk.store.add_decision(now(), "executor", "sell", trade["mint"], trade["symbol"],
                                     trade["exit_reason"], {"pnl_sol": trade["pnl_sol"], "pnl_pct": trade["pnl_pct"]})
        took = f", {speed['trigger_to_fill_ms']} ms" if speed and "trigger_to_fill_ms" in speed else ""
        self.emit("trade", f"SELL {trade['symbol']} {trade['pnl_pct']:+.2f}% ({trade['pnl_sol']:+.4f} SOL) "
                           f"— {order.reason}{took}", trade["mint"], trade["symbol"], pnl_sol=trade["pnl_sol"],
                  pnl_pct=trade["pnl_pct"], reason=trade["exit_reason"])
