# Phantom Desk — autonomous Solana memecoin desk (capital preservation first)

Six specialized agents with **hard handoffs**. A token can only reach the money through
`Scout → Filter → Analyst → Executor`, and every stage signs its output (HMAC) so the
Executor can prove a token passed *every* filter and scored ≥ 0.6 before it will trade.

```
 Scout ──Candidate──▶ Filter ──signed FilterPass──▶ Analyst ──signed Verdict──▶ Executor ◀──ExitOrder── Risk
   ▲                    │ kills + reasons              │ score + reasons            │ fills                 │ marks every 2s
   └── watchlist ◀──────┘                              └──────────────▶ Logger ◀────┴──────────────────────┘
                                                        (every trade & rejection, 4h recap → Telegram/Discord)
```

| Agent | Job | Hard rules it enforces |
|---|---|---|
| **Scout** | Real-time discovery: PumpPortal websocket (every pump.fun launch + graduation), GeckoTerminal new pools (Raydium, LaunchLab, PumpSwap, pump.fun), DexScreener new profiles. Keeps a watchlist and re-submits young tokens; feeds the rug ledger when it sees liquidity collapse. | never judges quality |
| **Filter** | Kills anything failing: **mint authority not revoked** (checked on-chain), **LP not locked/burned** (≥ 95%, liquidity-weighted), **top-10 holders > 20%** (pools excluded), **1h volume < $50k**, **dev linked to previous rugs** (RugCheck creator history + our own rug ledger + serial-dead-launch pattern). Extras: freeze authority, dangerous Token-2022 extensions, danger-level RugCheck risks, liquidity < $20k. | only all-pass is signed |
| **Analyst** | Scores 0–1 from liquidity, turnover, buy pressure, momentum, holder quality, contract hygiene, **website legit & tasty**, age. Caps for wash trading, insider clusters, parabolic tops, scam sites. | < 0.6 = no trade |
| **Executor** | Verifies both signatures + freshness, asks Risk, re-quotes live, rejects high price impact and **honeypots / hidden taxes** (buy→sell round-trip quote), then buys. Exits always processed first. | **max 8% of current balance per trade** |
| **Risk** | Marks positions on executable value (after exit costs) every ~2s with real Jupiter sell quotes. | **hard stop −6%**, **trail only after +12%** (6% below peak), no averaging down, no revenge trades (never re-enter a loser, cooldown after a loss, pause after 3 losses, −10% daily halt), emergency exit on liquidity pull |
| **Logger** | Writes every decision with its reasons to SQLite + log files; sends the **full recap every 4 hours**. | — |

The core rules are hard-coded ceilings in `desk/config.py`. `config.toml` can make them
stricter, never looser (e.g. `max_position_pct = 20` is clamped back to 8 and flagged
on screen).

## Website check ("legit & tasty")
If the pair lists a website (or the token metadata does), the Analyst fetches it and checks
HTTPS, whether the contract address is on the page, whether it links the same socials as the
listing, real content vs. parking/placeholder pages, scam & wallet-drainer phrases,
templates mass-reused across tokens, and domain age. With `ANTHROPIC_API_KEY` set,
Claude also reads the page and returns a structured legit/tasty score with red flags
(the page is treated as untrusted data; attempts to instruct the reviewer count as a red flag).

## Paper first, then live
* `mode = "paper"` (default): fills are simulated at **real live Jupiter quotes**, minus a 0.6%
  latency haircut and network/priority fees. Nothing touches a wallet.
* `mode = "live"` is **refused** until the paper record proves itself (default: ≥ 30 closed paper
  trades, positive paper P&L, max drawdown < 20%; see `[live_gate]`). It also needs
  `WALLET_PRIVATE_KEY` and `LIVE_CONFIRM=I_ACCEPT_THE_RISK` in `.env`. Check anytime with
  `python run.py gate`.
* Paper and live keep separate databases (`data/paper.db`, `data/live.db`).

## Run it 24/7 (no code needed)
On any small Ubuntu VPS (1 vCPU / 1 GB is enough), as root:

```bash
curl -fsSL https://raw.githubusercontent.com/nisiix/phantom-bot/main/deploy/install.sh | bash
```
(If the repo is private, `git clone` it yourself into `/opt/phantom-desk` and run
`bash deploy/install.sh` from there.)

That installs Docker, starts the desk in **paper mode**, and restarts it automatically after
crashes or reboots. Then:

| What | How |
|---|---|
| Watch the live terminal | `docker attach phantom-desk` — leave with **Ctrl-P Ctrl-Q** (Ctrl-C stops the desk) |
| Get the 4h recap on your phone | put `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (or `DISCORD_WEBHOOK_URL`) in `/opt/phantom-desk/.env`, then `docker compose up -d` |
| Read recaps on the server | `ls /opt/phantom-desk/data/reports` |
| Recap right now | `docker exec phantom-desk python run.py recap` |
| Is live unlocked? | `docker exec phantom-desk python run.py gate` |
| Go live (after the gate passes) | set `mode = "live"` in `config.toml`, fill the LIVE section of `.env`, `docker compose up -d` |

Without Docker: `pip install -r requirements.txt && python run.py` (add `--headless` for plain logs).

## Honest limits
* Memecoins can gap through a stop (a rug can take the price −90% between two polls).
  The −6% stop is enforced as fast as data allows, not guaranteed as a fill price; the
  liquidity-pull and honeypot guards exist for that reason, and sizing caps each loss at 8% of
  balance in the worst case.
* "Dev connected to previous rugs" uses RugCheck's creator history, the creator's prior
  launches, and a rug ledger the desk builds from what it observes. It doesn't trace funding wallets
  across hops.
* Free public APIs are rate-limited. For live trading, use a private RPC (`SOLANA_RPC_URL`).
* Paper results are an estimate; live slippage on thin pools can be worse.

## Tests
`python -m pytest -q` covers the mandate: config can't loosen limits, sizing can never exceed
8%, the −6% stop, trail only after +12%, no averaging down / revenge trades, losing-streak pause,
and the Executor refusing forged, tampered, expired or partial clearances.
