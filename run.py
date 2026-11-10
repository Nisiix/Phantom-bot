#!/usr/bin/env python3
"""PHANTOM DESK — autonomous Solana memecoin desk.

  python run.py                 start the desk (mode from config.toml, default paper)
  python run.py --headless      no full-screen UI, plain log lines (servers / docker logs)
  python run.py recap           print a recap of the last 4h right now
  python run.py gate            show whether the paper record unlocks live trading
  python run.py wallet          check the wallet key in .env (shows address + balance, never the key)
  python run.py research        which signals separated winners from losers (from the 60-min shadow data)
  python run.py controller      Controller report now: tests other settings on the logged data (proposals only)
  python run.py reset-paper     paper account back to the starting balance, profit stats zeroed (backup kept;
                                the 60-min follow-up data the Controller and QUANT learn from is kept)
  python run.py pages           publish the dashboard to GitHub Pages now (the desk must be running;
                                with [pages] enabled = true the desk does it by itself every 10 min)
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


def controller_report(cfg) -> str:
    """Offline Controller round on the saved data (the behaviour checks need the running bot)."""
    from desk.controller import build_report, reason_category, run_experiments
    store = Store(cfg.data_dir / f"{cfg.mode}.db")
    since = time.time() - cfg.controller.lookback_days * 86400
    cols = "start_ts,mint,score,decision,sim_net_pct,cost_pct,features,path"
    an = store.q(f"SELECT {cols} FROM shadow WHERE stage='analyst' AND features IS NOT NULL AND start_ts>=? "
                 "ORDER BY start_ts DESC LIMIT 6000", (since,))
    fi = store.q(f"SELECT {cols} FROM shadow WHERE stage='filter' AND features IS NOT NULL AND start_ts>=? "
                 "ORDER BY start_ts DESC LIMIT 20000", (since,))
    rej: dict = {}
    for r in store.q("SELECT mint,reason FROM decisions WHERE stage='executor' AND outcome IN ('reject','refuse') "
                     "AND ts>=?", (since,)):
        rej.setdefault(r["mint"], []).append(reason_category(r["reason"]))
    st = store.kv_get("controller_state", {}) or {}
    exp = run_experiments(cfg, an, fi, rej, st.get("best", {}))
    health = [{"level": "info", "agent": "Controller", "check": "verifiche sugli agenti",
               "detail": "disponibili solo con il bot in esecuzione (dashboard, sezione Controllore)"}]
    from desk.controller_memory import report_section
    return build_report(health, exp, cfg, st.get("trials", 0) + exp["tried"], st.get("rounds", 0) + 1,
                        report_section(store, cfg))


def reset_paper(cfg) -> str:
    """Paper account back to paper_starting_balance_sol with zeroed profit statistics.

    NEVER removes learning data (the bot's historical memory): shadow follow-ups and price paths, QUANT outcomes,
    decisions/events, rug ledger, Risk's per-token memory, Controller state, market history.
    Removes paper trades, open paper positions, the equity curve and the balance/streak state from the account
    (trades and equity are moved to archive tables: the live gate keeps counting them, so a reset never
    restarts or shortcuts the paper record needed for live). Keeps what the desk
    learns from (60-min shadow follow-ups, QUANT outcomes, decisions/events log, rug ledger, Controller state).
    A copy of the database is saved first. Stop the bot before running it."""
    import shutil
    path = cfg.data_dir / "paper.db"
    if not path.exists():
        return f"No paper database at {path}: nothing to reset."
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = cfg.data_dir / f"paper_backup_{stamp}.db"
    store = Store(path)
    store.db.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy2(path, backup)
    n_trades = store.q("SELECT COUNT(*) AS n FROM trades")[0]["n"]
    n_pos = store.q("SELECT COUNT(*) AS n FROM positions")[0]["n"]
    store.archive_and_clear_trades(time.time())     # archived trades keep counting for the live gate
    # The per-token memory of Risk (tokens we lost on are never bought again) is learning, not a profit stat:
    # keep it, reset only the losing streak and the daily counters.
    risk = store.kv_get("risk_state", {}) or {}
    store.kv_set("risk_state", {"history": risk.get("history", {})})
    for key in ("cash_sol", "starting_cash", "realized_sol", "last_recap_ts"):
        store.db.execute("DELETE FROM kv WHERE key=?", (key,))
    store.kv_set("starting_cash", cfg.paper_starting_balance_sol)
    store.kv_set("cash_sol", cfg.paper_starting_balance_sol)
    store.kv_set("realized_sol", 0.0)
    store.kv_set("paper_reset_ts", time.time())
    return (f"Paper account reset to {cfg.paper_starting_balance_sol:g} SOL: removed {n_trades} trades, "
            f"{n_pos} open positions and the equity curve. Backup: {backup.name}. "
            "Learning data (60-min follow-ups, QUANT outcomes) kept; the live-gate record keeps every trade.")


def main() -> int:
    # Windows consoles/pipes may default to cp1252: never crash on token names or chart glyphs.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Phantom Desk")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "recap", "gate", "wallet", "research", "controller", "reset-paper", "pages"])
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
                             trades=[t for t in store.archived_trades() + store.all_trades()
                                     if (t["entry_ts"] or 0) >= since]))
        return 0

    if args.command == "reset-paper":
        print(reset_paper(cfg))
        return 0

    if args.command == "pages":
        from desk.pages import publish_from_running_desk
        try:
            print(publish_from_running_desk(cfg))
        except (RuntimeError, OSError, ValueError) as e:
            print("GitHub Pages: not published:", e, file=sys.stderr)
            return 1
        return 0

    if args.command == "controller":
        print(controller_report(cfg))
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
        print("Desk stopped (Ctrl+C).", file=sys.stderr)
    except Exception:
        # A real crash: keep the traceback on disk, the console window may be closed before anyone reads it.
        import traceback
        crash = cfg.data_dir / cfg.mode / "crash.log"
        try:
            crash.parent.mkdir(parents=True, exist_ok=True)
            with crash.open("a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} desk crashed ===\n{traceback.format_exc()}")
            print(f"\nDesk crashed: the error is saved in {crash}", file=sys.stderr)
        except OSError:
            pass
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
