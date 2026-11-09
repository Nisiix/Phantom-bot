"""EXECUTOR — the only agent that touches money.

It will only buy a token when ALL of these hold:
  * the Verdict's Analyst signature verifies, and the embedded Filter clearance
    signature verifies, and that clearance shows every filter check passed;
  * both clearances are fresh (clearance_ttl_seconds) and the score is >= 0.6;
  * Risk approves the entry (no averaging down, no revenge, cooldowns, limits);
  * the live buy quote's price impact and the buy->sell round-trip cost are acceptable
    (catches honeypots, hidden taxes and thin books right before execution);
  * QUANT: expected value after the measured round-trip cost is positive, using calibrated
    probabilities of +12% first / -6% first / neither within 60 min (blocks only, never forces).
Size = max_position_pct (<= 8%, hard ceiling) of the CURRENT balance. Never more.
Exits ordered by Risk are always processed before new entries.
"""
from __future__ import annotations

import asyncio
import math

from ..clients.jupiter import SOL_MINT
from ..config import HARD_MAX_POSITION_PCT, HARD_MIN_SCORE
from ..models import ExitOrder, FilterPass, Position, Verdict, now
from ..portfolio import AveragingDownError
from ..quant import label_outcome, outcome_row
from .base import Agent


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
        # Swaps are serialised (live fills are measured from wallet balance deltas, so a buy and a
        # sell must not settle at the same time). Only the swap itself holds it, not the checks.
        self._swap_lock = asyncio.Lock()

    async def run(self) -> None:
        self.status("idle")
        self.emit("info", f"Executor online [{self.broker.mode.upper()}]: max {self.e.max_position_pct}% "
                          f"of balance per trade, max {self.e.max_open_positions} positions")
        await asyncio.gather(self.supervised("executor-exits", self.exit_loop),
                             self.supervised("executor-entries", self.loop))

    async def exit_loop(self) -> None:
        """Exits run on their own loop: a stop is never queued behind an entry's quotes and checks."""
        while not self.desk.stop.is_set():
            try:
                order = await asyncio.wait_for(self.desk.exits.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            await self.handle_exit(order)

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
        if t - fp.issued_at > ttl or t - v.issued_at > ttl:
            return False, f"clearance expired (> {ttl:.0f}s old)"
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
        self.desk.in_flight.add(v.mint)
        self.status("working")
        try:
            await self._enter(v)
        finally:
            self.desk.in_flight.discard(v.mint)

    async def _enter(self, v: Verdict) -> None:
        pf = self.desk.portfolio
        cash = await self.broker.cash()
        if not pf.positions and self.broker.mode == "live":
            pf.set_cash(cash)
        size = position_size(cash, self.e.max_position_pct)
        if size < self.e.min_trade_sol:
            return self.reject(v, f"size {size:.4f} SOL below minimum {self.e.min_trade_sol} (balance {cash:.3f})")

        decimals = v.clearance.report.rug.get("decimals")
        if decimals is None:
            info = await self.desk.rpc.mint_info(v.mint)
            decimals = int((info or {}).get("decimals") or 6)
        decimals = int(decimals)

        snap = await self.desk.dex.snapshot(v.mint)
        if snap is None:
            return self.reject(v, "market data vanished before execution")
        lamports = int(size * 1e9)
        buy_q = await self.desk.jup.quote(SOL_MINT, v.mint, lamports, self.e.slippage_bps)
        if not buy_q:
            return self.reject(v, "no Jupiter buy route")
        out_tokens = int(buy_q["outAmount"]) / 10 ** decimals
        if snap.price_native > 0:
            fair = size / snap.price_native
            impact = max(0.0, (1 - out_tokens / fair) * 100)
        else:
            impact = float(buy_q.get("priceImpactPct") or 0) * 100
        if impact > self.e.max_entry_price_impact_pct:
            return self.reject(v, f"entry price impact {impact:.2f}% > {self.e.max_entry_price_impact_pct}%")
        sell_q = await self.desk.jup.quote(v.mint, SOL_MINT, int(buy_q["outAmount"]), self.e.slippage_bps)
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
        qv = quant.evaluate(v.score, cost)
        if self.cfg.quant.enabled and not qv.passes:
            self.state.bump("ev_block")
            return self.reject(v, qv.reason)

        async with self._swap_lock:
            # Exits run concurrently now: a loss may have landed while we were quoting.
            # Ask Risk again right before the swap (cooldown / streak pause / daily halt / max positions).
            ok, why = self.risk.approve_entry(v.mint, own_order=True)
            if not ok:
                return self.reject(v, f"Risk (re-check before swap): {why}")
            fill = await self.broker.buy(v.mint, size, decimals, buy_q)
        if not fill.ok or fill.tokens <= 0:
            return self.reject(v, f"buy failed: {fill.error or 'no tokens received'} {fill.tx}")

        rep = v.clearance.report
        reason = (f"score {v.score:.2f} | {'; '.join(v.rationale[:5])} | filters: "
                  + ", ".join(f"{c.name}={c.value}" for c in rep.checks)
                  + f" | size {size:.4f} SOL = {self.e.max_position_pct}% of {cash:.3f} | impact {impact:.2f}% "
                    f"| round-trip {rt_loss:.2f}% | {qv.short()}")
        pos = Position(
            mint=v.mint, symbol=v.symbol, tokens=fill.tokens, decimals=decimals, cost_sol=fill.sol,
            entry_price_sol=fill.sol / fill.tokens, entry_ts=now(), entry_liquidity_usd=snap.liquidity_usd,
            exit_cost_pct=max(0.0, rt_loss / 2), score=v.score, entry_reason=reason,
            mark_value_sol=fill.sol * (1 - max(0.0, rt_loss) / 100), tx_in=fill.tx,
            rt_cost_pct=cost, p_up=qv.p["up"], p_down=qv.p["down"], p_neither=qv.p["neither"], ev_pct=qv.ev_pct,
        )
        pos.peak_value_sol = pos.mark_value_sol
        pos.last_price_ts = now()
        pos.last_liquidity_usd = snap.liquidity_usd
        try:
            pf.open(pos)
        except AveragingDownError as e:
            return self.reject(v, str(e))
        if self.broker.mode == "live":
            pf.set_cash(await self.broker.cash())
        self.state.bump("buys")
        if self.desk.shadow is not None:
            self.desk.shadow.mark_traded(v.mint)
        self.desk.store.add_decision(now(), "executor", "buy", v.mint, v.symbol, reason,
                                     {"breakdown": v.breakdown, "website": v.website.get("url"), "tx": fill.tx})
        self.emit("trade", f"BUY {fill.sol:.4f} SOL -> {fill.tokens:,.0f} {v.symbol} (score {v.score:.2f}, "
                           f"impact {impact:.1f}%, rt {rt_loss:.1f}%, EV {qv.ev_pct:+.1f}%)", v.mint, v.symbol, reason=reason, tx=fill.tx)

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
            tokens = pos.tokens
            if self.broker.mode == "live":
                held = await self.broker.token_holdings(order.mint)
                if held > 0:
                    tokens = held  # sell what is actually in the wallet
            raw = int(tokens * 10 ** pos.decimals)
            quote = None
            # Capital preservation: widen slippage step by step to make sure we get OUT.
            for bps in (self.e.slippage_bps, 800, 1500, 3000):
                quote = await self.desk.jup.quote(order.mint, SOL_MINT, raw, bps, lane="risk")
                if quote:
                    break
            if not quote:
                if order.reason in ("LIQUIDITY_PULL", "DATA_LOSS") and self.broker.mode == "paper":
                    trade = pf.close(order.mint, 0.0, f"{order.reason}: no route, marked total loss")
                    self._finish(trade, order, pos)
                    return
                self.emit("error", f"EXIT BLOCKED ({order.reason}): no sell route, retrying", order.mint, pos.symbol)
                self.risk.exit_failed(order.mint)
                return
            async with self._swap_lock:
                fill = await self.broker.sell(order.mint, tokens, pos.decimals, quote)
            if not fill.ok:
                self.emit("error", f"SELL FAILED ({order.reason}): {fill.error}, retrying", order.mint, pos.symbol)
                self.risk.exit_failed(order.mint)
                return
            trade = pf.close(order.mint, fill.sol, f"{order.reason}: {order.detail}", fill.tx)
            if self.broker.mode == "live":
                pf.set_cash(await self.broker.cash())
            self._finish(trade, order, pos)
        finally:
            self.desk.in_flight.discard(order.mint)

    def _finish(self, trade: dict, order: ExitOrder, pos: Position) -> None:
        self.risk.exit_done(order.mint)
        self.risk.record_result(trade)
        # QUANT learning sample: which barrier did this trade touch first within the horizon?
        label = label_outcome(pos.entry_ts, trade["exit_ts"], pos.hit_up_ts, pos.hit_down_ts, trade["pnl_pct"],
                              self.cfg.quant.horizon_minutes * 60, self.cfg.risk.hard_stop_pct)
        row = outcome_row(trade, pos, label, self.cfg.mode)
        self.desk.store.add_outcome(row)
        self.desk.quant.add(row)
        self.state.bump("sells")
        self.state.bump("wins" if trade["pnl_sol"] > 0 else "losses")
        self.desk.store.add_decision(now(), "executor", "sell", trade["mint"], trade["symbol"],
                                     trade["exit_reason"], {"pnl_sol": trade["pnl_sol"], "pnl_pct": trade["pnl_pct"]})
        self.emit("trade", f"SELL {trade['symbol']} {trade['pnl_pct']:+.2f}% ({trade['pnl_sol']:+.4f} SOL) "
                           f"— {order.reason}", trade["mint"], trade["symbol"], pnl_sol=trade["pnl_sol"],
                  pnl_pct=trade["pnl_pct"], reason=trade["exit_reason"])
