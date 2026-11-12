"""Typed hand-off messages between agents, and the clearance system.

Hard handoffs:  Scout -> Candidate -> Filter -> FilterPass -> Analyst ->
Verdict -> Executor. Each stage signs its output with its own role key
(HMAC-SHA256). The Executor only trades a Verdict whose Analyst signature AND
embedded Filter signature both verify, are fresh, and show every filter check
passed. A token can't reach the Executor by any other path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def now() -> float:
    return time.time()


# ── Clearance authority ──────────────────────────────────────────────────────
class ClearanceAuthority:
    """Issues per-role signers and verifies signatures. Keys live only in memory."""

    ROLES = ("filter", "analyst")

    def __init__(self) -> None:
        self._keys = {r: os.urandom(32) for r in self.ROLES}

    def signer(self, role: str) -> "RoleSigner":
        return RoleSigner(role, self._keys[role])

    def verify(self, role: str, payload: str, sig: str) -> bool:
        key = self._keys.get(role)
        if key is None or not sig:
            return False
        expected = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)


class RoleSigner:
    def __init__(self, role: str, key: bytes) -> None:
        self.role = role
        self._key = key

    def sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()


# ── Messages ─────────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    mint: str
    symbol: str = "?"
    name: str = ""
    source: str = ""
    discovered_at: float = field(default_factory=now)
    creator: Optional[str] = None
    pair_address: Optional[str] = None
    attempts: int = 0


@dataclass
class Check:
    name: str
    passed: bool
    value: Any = None
    threshold: Any = None
    detail: str = ""
    permanent: bool = False  # failure is structural -> blacklist token

    def describe(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        extra = f" ({self.detail})" if self.detail else ""
        return f"{mark} {self.name}: {self.value} vs {self.threshold}{extra}"


@dataclass
class MarketSnapshot:
    """Aggregated DexScreener view of a token across its Solana pairs."""
    mint: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    price_usd: float
    price_native: float          # SOL per token (0 if quote is not SOL)
    liquidity_usd: float
    volume_h1: float
    volume_m5: float
    buys_h1: int
    sells_h1: int
    buys_m5: int
    sells_m5: int
    change_m5: float
    change_h1: float
    change_h6: float
    fdv: float
    market_cap: float
    pair_created_at: float       # epoch seconds
    websites: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    pair_count: int = 1
    # The pool every number above was observed on (pair_address) and what it trades against. The Executor buys
    # in THIS pool: a token can have other pools (other DEX, USDC instead of SOL) with little or no liquidity.
    quote_mint: str = ""
    quote_symbol: str = ""
    pair_liquidity_usd: float = 0.0
    # Every Solana pool of the token: {"address", "dex", "quote_mint", "quote_symbol", "liquidity_usd"}.
    pools: list[dict] = field(default_factory=list)

    def pool(self, address: str) -> dict | None:
        return next((p for p in self.pools if p.get("address") == address), None)

    @property
    def age_minutes(self) -> float:
        return max(0.0, (now() - self.pair_created_at) / 60) if self.pair_created_at else 0.0


@dataclass
class FilterReport:
    mint: str
    symbol: str
    passed: bool
    checks: list[Check]
    market: Optional[MarketSnapshot] = None
    rug: dict = field(default_factory=dict)     # condensed RugCheck facts for the Analyst
    creator: Optional[str] = None
    ts: float = field(default_factory=now)
    source: str = ""

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def permanent_fail(self) -> bool:
        return any(c.permanent for c in self.failed)

    def reason_text(self) -> str:
        if self.passed:
            return "all filters passed"
        return "; ".join(f"{c.name}: {c.detail or c.value}" for c in self.failed)

    def checks_digest(self) -> str:
        blob = json.dumps([(c.name, c.passed) for c in self.checks], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class FilterPass:
    """Signed clearance that a token passed EVERY filter check."""
    report: FilterReport
    issued_at: float
    signature: str

    @staticmethod
    def payload(report: FilterReport, issued_at: float) -> str:
        all_passed = all(c.passed for c in report.checks) and report.passed
        return f"filter|{report.mint}|{all_passed}|{report.checks_digest()}|{issued_at:.3f}"


@dataclass
class Verdict:
    mint: str
    symbol: str
    score: float
    breakdown: dict[str, float]
    rationale: list[str]
    website: dict
    clearance: FilterPass
    issued_at: float
    signature: str
    trade: bool
    features: dict = field(default_factory=dict)   # numbers seen at decision time (for the Controller's model)

    @staticmethod
    def payload(mint: str, score: float, trade: bool, filter_sig: str, issued_at: float) -> str:
        return f"analyst|{mint}|{score:.4f}|{trade}|{filter_sig}|{issued_at:.3f}"


@dataclass
class ExitOrder:
    mint: str
    reason: str
    detail: str = ""
    ts: float = field(default_factory=now)
    fraction: float = 1.0         # share of the held tokens to sell (< 1 = partial sale, SCALE_OUT; the rest stays open)


@dataclass
class Position:
    mint: str
    symbol: str
    tokens: float                 # UI amount
    decimals: int
    cost_sol: float               # SOL actually spent (incl. fees)
    entry_price_sol: float        # cost_sol / tokens
    entry_ts: float
    entry_liquidity_usd: float
    exit_cost_pct: float          # measured round-trip friction for honest marking
    score: float
    entry_reason: str
    mark_value_sol: float = 0.0
    peak_value_sol: float = 0.0
    trailing_active: bool = False
    last_price_ts: float = 0.0
    last_liquidity_usd: float = 0.0
    tx_in: str = ""
    # QUANT bookkeeping: first time each barrier was touched, cost and forecast at entry
    hit_up_ts: float = 0.0
    hit_down_ts: float = 0.0
    rt_cost_pct: float = 0.0
    p_up: float = 0.0
    p_down: float = 0.0
    p_neither: float = 0.0
    ev_pct: float = 0.0
    regime: dict = field(default_factory=dict)   # market regime at entry (measurement only)
    # Profit lock (desk/exits.py) and exit bookkeeping, stored with the trade for the Controller.
    lock_touch_s: float | None = None    # seconds after the buy when the arming level was first reached
    lock_armed_on: float = 0.0           # net % that armed the lock (0 = not armed)
    lock_armed_ts: float = 0.0
    lock_at: float = 0.0                 # net % the lock sells at
    lock_buffer: float = 0.0             # sell buffer in use when it armed
    lock_keep: float = 0.0               # share of the net peak the lock keeps (0 = fixed at lock_at)
    lock_level: float = 0.0              # highest net % the stop was raised to (rises with the price, never falls)
    lock_speed: float = 0.0              # fastest rise seen while armed, % points per minute (for the records)
    trigger_pnl_pct: float | None = None # marked net % when Risk issued the exit (slippage = this - realized)
    exit_rules: dict = field(default_factory=dict)   # exit settings at entry (stop, trailing, time, lock)
    # Partial sale (user's rule 2026-11-12, [risk] scale_out_*): once done, tokens / cost_sol / values describe only
    # the part still held (same price per token, so every % and every stop is unchanged); the sold part is here and
    # is added back on the final close, so the trade is recorded whole.
    scaled_out: bool = False
    partial_cost_sol: float = 0.0        # cost of the tokens already sold
    partial_proceeds_sol: float = 0.0    # SOL received for them

    @property
    def pnl_pct(self) -> float:
        if self.cost_sol <= 0:
            return 0.0
        return (self.mark_value_sol / self.cost_sol - 1) * 100

    @property
    def peak_pnl_pct(self) -> float:
        if self.cost_sol <= 0:
            return 0.0
        return (self.peak_value_sol / self.cost_sol - 1) * 100

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Event:
    agent: str
    kind: str              # info | reject | pass | score | trade | exit | risk | error | recap | warn
    message: str
    mint: str = ""
    symbol: str = ""
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=now)
