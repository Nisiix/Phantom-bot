#!/usr/bin/env python3
"""PHANTOM DESK — autonomous Solana memecoin desk.

  python run.py                 start the desk (mode from config.toml, default paper)
  python run.py --headless      no full-screen UI, plain log lines (servers / docker logs)
  python run.py recap           print a recap of the last 4h right now
  python run.py gate            show whether the paper record unlocks live trading
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


def main() -> int:
    # Windows consoles/pipes may default to cp1252: never crash on token names or chart glyphs.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Phantom Desk")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "recap", "gate"])
    ap.add_argument("--mode", choices=["paper", "live"], help="override mode from config.toml")
    ap.add_argument("--headless", action="store_true", help="plain log output instead of the live terminal")
    ap.add_argument("--config", help="path to config.toml")
    args = ap.parse_args()
    cfg = load_config(args.config, args.mode)
    for w in cfg.warnings:
        print("!", w, file=sys.stderr)

    if args.command == "gate":
        ok, msgs = live_gate_status(Store(cfg.data_dir / "paper.db"), cfg.live_gate)
        print(("LIVE UNLOCKED: " if ok else "LIVE LOCKED: ") + "; ".join(msgs))
        return 0 if ok else 1

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
