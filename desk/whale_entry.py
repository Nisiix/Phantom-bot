"""WHALE ENTRY — when to buy a token approved because a smart wallet holds it (user 2026-11-12).

A token the Analyst approved thanks to the Whale checker (smart wallets weigh 51% of its score) is not bought at
any price: we must not become the smart wallet's exit liquidity, nor buy the top of a candle.

  * reference = the smart wallets' own average buy price (SOL per token, read on-chain from their token account);
  * buy only while our price is at most `max_entry_premium_pct` (30%) above that reference AND the token is not
    spiking (5-minute change <= `whale_max_m5_pct`);
  * otherwise WAIT, watching the price, up to `whale_wait_minutes`, for it to come back near the smart entry;
  * if one of those smart wallets SELLS while we wait, the token is dropped for `whale_sold_block_hours`, whatever
    the other rules say: they are leaving, we don't go in. In that time it is bought only if OTHER smart wallets
    (not the sellers) hold it, near their own buy price (user 2026-11-12);
  * a liquidity drop of [entry] max_liq_drop_pct while waiting also cancels (a rug is not a discount).

The buy itself still needs a fresh Filter + Analyst clearance and every Executor check (Risk, price impact,
round trip / honeypot, QUANT): waiting can only delay or drop a buy, never skip a check.
"""
from __future__ import annotations


def premium_pct(price_sol: float, ref_sol: float) -> float | None:
    """How far our price is above the smart wallets' average buy price, in %."""
    if not price_sol or not ref_sol or price_sol <= 0 or ref_sol <= 0:
        return None
    return (price_sol / ref_sol - 1) * 100


def decide(sec: float, price_sol: float, ref_sol: float, change_m5: float, liq_ratio: float | None,
           sold: list[str], c, max_liq_drop_pct: float = 20.0) -> tuple[str, str]:
    """One look at a waiting whale token. Returns (state, why): "buy", "wait", "skip" (time up), "cancel"
    (liquidity leaving) or "sold" (a smart wallet sold: drop and block)."""
    if sold:
        return "sold", f"il wallet bravo {sold[0][:6]} ha venduto durante l'attesa: token scartato"
    if liq_ratio is not None and liq_ratio < 1 - max_liq_drop_pct / 100:
        return "cancel", f"liquidità scesa del {(1 - liq_ratio) * 100:.0f}% durante l'attesa"
    prem = premium_pct(price_sol, ref_sol)
    limit = float(c.max_entry_premium_pct)
    if prem is not None and prem <= limit and change_m5 <= float(c.whale_max_m5_pct):
        return "buy", (f"prezzo {prem:+.0f}% rispetto all'acquisto dei wallet bravi (limite +{limit:g}%), "
                       f"{change_m5:+.1f}% in 5 min")
    if sec > float(c.whale_wait_minutes) * 60:
        return "skip", (f"in {c.whale_wait_minutes:g} min il prezzo non è tornato entro +{limit:g}% dall'acquisto "
                        f"dei wallet bravi (ora {prem:+.0f}%)" if prem is not None else
                        f"in {c.whale_wait_minutes:g} min nessun prezzo confrontabile")
    if prem is None:
        return "wait", "prezzo non confrontabile, aspetto"
    if prem > limit:
        return "wait", f"prezzo {prem:+.0f}% sopra l'acquisto dei wallet bravi (limite +{limit:g}%): aspetto che scenda"
    return "wait", f"picco in corso ({change_m5:+.1f}% in 5 min, limite +{c.whale_max_m5_pct:g}%): aspetto"
