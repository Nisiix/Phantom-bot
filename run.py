#!/usr/bin/env python3
"""PHANTOM DESK — autonomous Solana memecoin desk.

  python run.py                 start the desk (mode from config.toml, default paper)
  python run.py --headless      no full-screen UI, plain log lines (servers / docker logs)
  python run.py recap           print a recap of the last 4h right now
  python run.py gate            show whether the paper record unlocks live trading
  python run.py wallet          check the wallet key in .env (shows address + balance, never the key)
  python run.py research        which signals separated winners from losers (from the 60-min shadow data)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from desk.app import LiveRefused, run_desk
from desk.config import load_config
from desk.portfolio import Portfolio
from desk.quant import QuantModel
from desk.recap import build_recap, live_gate_status
from desk.shadow import shadow_to_outcome
from desk.store import Store


async def check_wallet(cfg) -> int:
    """Validate WALLET_PRIVATE_KEY without trading: public address, SOL balance, live readiness."""
    from desk.app import LIVE_PHRASE
    from desk.brokers.live import load_keypair
    from desk.clients.http import Http
    from desk.clients.solana_rpc import SolanaRPC

    secret = cfg.secrets.wallet_private_key
    if not secret:
        print("No wallet: WALLET_PRIVATE_KEY is empty in .env (fine for paper mode).")
        return 1
    try:
        pubkey = str(load_keypair(secret).pubkey())
    except Exception:  # noqa: BLE001 - never echo the secret or parser details
        print("INVALID key: WALLET_PRIVATE_KEY is not a valid Solana private key.\n"
              "Paste the private key exported from Phantom (a long base58 string), nothing else.")
        return 1
    print(f"Wallet OK  - address: {pubkey}")
    http = Http()
    try:
        sol = await SolanaRPC(http, cfg.secrets.rpc_url).sol_balance(pubkey)
        print(f"Balance    - {sol:.4f} SOL (tradeable after the {cfg.executor.fee_reserve_sol} SOL fee reserve: "
              f"{max(0.0, sol - cfg.executor.fee_reserve_sol):.4f} SOL)")
    except Exception as e:  # noqa: BLE001
        print(f"Balance    - could not read ({e.__class__.__name__}); check SOLANA_RPC_URL / internet")
    finally:
        await http.close()
    ok, msgs = live_gate_status(Store(cfg.data_dir / "paper.db"), cfg.live_gate)
    print(f"Paper gate - {'PASSED' if ok else 'not yet'}: {'; '.join(msgs)}")
    print(f"Confirm    - LIVE_CONFIRM {'set' if cfg.secrets.live_confirm == LIVE_PHRASE else 'NOT set'} "
          f"| mode in config.toml: {cfg.mode}")
    return 0


def main() -> int:
    # Windows consoles/pipes may default to cp1252: never crash on token names or chart glyphs.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Phantom Desk")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "recap", "gate", "wallet", "research"])
    ap.add_argument("--mode", choices=["paper", "live"], help="override mode from config.toml")
    ap.add_argument("--headless", action="store_true", help="plain log output instead of the live terminal")
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("--stage", choices=["analyst", "filter"], default="analyst",
                    help="research: tokens scored by the Analyst (default) or everything the Filter saw")
    ap.add_argument("--days", type=float, default=0, help="research: only the last N days (0 = all)")
    args = ap.parse_args()
    cfg = load_config(args.config, args.mode)
    for w in cfg.warnings:
        print("!", w, file=sys.stderr)

    if args.command == "gate":
        ok, msgs = live_gate_status(Store(cfg.data_dir / "paper.db"), cfg.live_gate)
        print(("LIVE UNLOCKED: " if ok else "LIVE LOCKED: ") + "; ".join(msgs))
        return 0 if ok else 1

    if args.command == "research":
        from desk.research import build_research
        store = Store(cfg.data_dir / f"{cfg.mode}.db")
        since = time.time() - args.days * 86400 if args.days else 0
        print(build_research(store.shadow_research_rows(args.stage, since), args.stage,
                             trades=[t for t in store.all_trades() if (t["entry_ts"] or 0) >= since]))
        return 0

    if args.command == "wallet":
        return asyncio.run(check_wallet(cfg))

    if args.command == "recap":
        store = Store(cfg.data_dir / f"{cfg.mode}.db")
        pf = Portfolio(store, cfg.mode, cfg.paper_starting_balance_sol)
        t1 = time.time()
        quant = QuantModel(min_samples_to_enforce=cfg.quant.min_samples_to_enforce,
                           min_ev_pct=cfg.quant.min_ev_pct, prior_strength=cfg.quant.prior_strength)
        quant.load([{**o, "weight": 1.0, "source": "trade"} for o in store.outcomes()]
                   + [shadow_to_outcome(r, cfg.quant.shadow_weight) for r in store.shadow_samples()])
        print(build_recap(store, pf, cfg.mode, t1 - cfg.recap.interval_hours * 3600, t1, quant=quant))
        return 0

    try:
        asyncio.run(run_desk(cfg, use_tui=not args.headless))
    except LiveRefused as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
