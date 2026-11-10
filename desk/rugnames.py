"""Rugged-name ledger: the "hype name factory" guard.

Seen in paper (2026-11-10): the same hype names were relaunched again and again on new mints (GTA 6 on 8 mints,
Rockstar on 6), each time with a fresh, "clean" dev wallet, so the wallet rug ledger never caught them. Once a
name or symbol has rugged, its relaunches within [filter] rugged_name_days are killed by the Filter.
"""
from __future__ import annotations

import re
import time

MIN_KEY_LEN = 3          # shorter keys ("AI", "X") would match far too much
SUBSTRING_KEY_LEN = 4    # a rugged key this long also matches inside a longer name ("gta6" in "gta6coin")


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def name_keys(symbol: str | None, name: str | None) -> set[str]:
    return {k for k in (norm(symbol), norm(name)) if len(k) >= MIN_KEY_LEN}


def record(store, mint: str, symbol: str | None, name: str | None, reason: str, ts: float | None = None) -> None:
    for k in name_keys(symbol, name):
        store.add_rugged_name(k, mint, symbol or "", reason[:160], ts)


def match(ledger: dict[str, dict], mint: str, symbol: str | None, name: str | None) -> dict | None:
    """The ledger row a token's name/symbol matches (another mint only), or None."""
    keys = name_keys(symbol, name)
    if not keys:
        return None
    for lk, row in ledger.items():
        if row.get("mint") == mint:
            continue
        if lk in keys or (len(lk) >= SUBSTRING_KEY_LEN and any(lk in k for k in keys)):
            return row
    return None


def seed_from_trades(store, loss_pct: float, max_hold_s: float = 3600) -> int:
    """Past trades (current + archived by paper resets) that lost >= loss_pct within an hour = rugs."""
    n = 0
    rows = store.all_trades() + (store.archived_trades() if hasattr(store, "archived_trades") else [])
    for t in rows:
        if t.get("pnl_pct") is None or t["pnl_pct"] > -loss_pct:
            continue
        if (t.get("exit_ts") or 0) - (t.get("entry_ts") or 0) > max_hold_s:
            continue
        record(store, t["mint"], t.get("symbol"), None, f"our trade lost {t['pnl_pct']:.0f}%", t.get("exit_ts"))
        n += 1
    return n


class RuggedNames:
    """Cached view of the recent part of the ledger (re-read at most every 30 s)."""

    def __init__(self, store, days: float) -> None:
        self.store, self.days = store, days
        self._cache: dict[str, dict] = {}
        self._read = 0.0

    def ledger(self) -> dict[str, dict]:
        t = time.time()
        if t - self._read > 30:
            self._read = t
            self._cache = self.store.rugged_names(t - self.days * 86400)
        return self._cache

    def invalidate(self) -> None:
        self._read = 0.0

    def match(self, mint: str, symbol: str | None, name: str | None) -> dict | None:
        if self.days <= 0:
            return None
        return match(self.ledger(), mint, symbol, name)
