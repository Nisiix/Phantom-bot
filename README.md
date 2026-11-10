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
| **Scout** | Real-time discovery: PumpPortal websocket (every pump.fun launch + graduation), GeckoTerminal new + trending pools on pump.fun / PumpSwap, DexScreener new profiles. By default only pump.fun tokens are traded (`[scout] venues = "pumpfun"`); `venues = "all"` adds Raydium / LaunchLab / letsbonk pools. Keeps a watchlist and re-submits young tokens (every 20-120 s for 3 h, restarted at graduation); a dropped token goes to a 24 h "second look" archive and comes back on the watchlist if it trades again (`[scout] revisit*`), killed tokens never do; feeds the rug ledger when it sees liquidity collapse. | never judges quality |
| **Filter** | Kills anything failing: **mint authority not revoked** (checked on-chain), **LP not locked/burned** (≥ 95% of the lockable pools, liquidity-weighted; Meteora DLMM / Raydium CLMM / Orca pools have no LP token and may hold at most `max_unlockable_liquidity_pct` = 50% of liquidity, set it to 0 under `[filter]` for the old strict rule; LP locks and concentrated-pool owners are also read on-chain, see `desk/onchain_lp.py`: liquidity provided by the dev, its funder, a top holder or a known rugger is rejected. Owners of NFT positions (DAMM v2 / CLMM / Orca) are checked only with a private `SOLANA_RPC_URL`), **top-10 holders > 20%** (pools excluded), **1h volume < $50k** (tokens younger than 4h: half their liquidity, at most $50k, at the same pace in their first hour, never under $5k/h), **dev linked to previous rugs** (RugCheck creator history + our own rug ledger + serial-dead-launch pattern). Extras: freeze authority, dangerous Token-2022 extensions, danger-level RugCheck risks, liquidity < $20k. | only all-pass is signed |
| **Analyst** | Scores 0–1 from liquidity, turnover, buy pressure, momentum, holder quality, contract hygiene, **website legit & tasty**, age. Caps for wash trading, insider clusters, parabolic tops, scam sites. Age-aware: under 4h old the price change since launch is not held against the token (momentum scaled to its age, only buying a +60%-in-5-min candle is capped); older tokens are capped from +200% in 1h or +300% in 6h (they may have pumped already). Young tokens also get a **traction** score: holders gained per minute, volume pace now vs the hour, average trade size (not dust bots, not one whale) and how much the dev still holds. | < 0.6 = no trade |
| **Executor** | Verifies both signatures + freshness, asks Risk, re-quotes live, rejects high price impact and **honeypots / hidden taxes** (buy→sell round-trip quote), then buys. Exits always processed first. | **max 8% of current balance per trade** |
| **Risk** | Marks positions on executable value (after exit costs) every ~2s with real Jupiter sell quotes. | **hard stop −6%**, **trail only after +12%** (6% below peak), no averaging down, no revenge trades (never re-enter a loser, no global cooldown after a loss, no pause after a losing streak (optional), −30% daily halt in live only), emergency exit on liquidity pull |
| **Logger** | Writes every decision with its reasons to SQLite + log files; sends the **full recap every 4 hours**. | — |
| **Controller** (Controllore) | Watches the other agents (alive, making progress, hard rules respected on every position and trade) and every 10 minutes tests other settings offline on the tokens already followed for 60 minutes. Applies the improvements it confirms by itself (paper by default), remembers why, re-checks them and rolls back what new data disproves. Never trades, no API calls. | hard limits always apply |

The core rules are hard-coded ceilings in `desk/config.py`. `config.toml` can make them
stricter, never looser (e.g. `max_position_pct = 20` is clamped back to 8 and flagged
on screen).

## QUANT — expected value after costs (self-calibrating)
Before every buy the Executor asks QUANT for P(**+12% first**), P(**−6% first**) and P(**neither
within 60 min**) for the token's score bucket, plus the typical return of each scenario, and computes
`EV = Σ P·R − measured round-trip cost`. The trade is blocked unless EV > 0. QUANT can only block,
never force a trade or loosen a rule. Its probabilities start at conservative priors and are
re-estimated from data (two-level shrinkage, so a handful of outcomes can't swing it). It stays in
**warm-up** (it computes and logs EV but doesn't block) until 30 effective outcomes exist.

## Shadow tracking — even skipped tokens become data
The Logger follows **every evaluated token, bought or skipped, for 60 minutes** and records which
barrier was hit first, on two sets:
* desk barriers **+12% / −6%** (net of estimated cost, like the desk's own P&L), plus a simulation of
  the desk's exit policy on the price path;
* model barriers **+60% / −30%** on raw price.

Analyst-scored tokens that weren't bought feed QUANT at half weight, so calibration also covers scores
below 0.6 and shows whether a higher score really means better odds. The 4h recap reports calibration
(Brier score vs. the naive base rate), outcomes by decision and by discovery source, and
"missed winners" (skipped tokens that hit +60% first) with the rule that rejected them.

## Research — which signals actually worked (`python run.py research`)
Every shadow row also stores what the desk saw at decision time (market data, holders, the Analyst's
component scores). `python run.py research` (or `start.bat research`) splits the followed tokens into
low / mid / high thirds for each signal and shows how often each third hit +12% before −6% and the
average result of the desk's exit policy on the real price path, with a 95% interval; splits whose
intervals overlap are marked as noise. It also breaks results down by discovery source, time of day
and score threshold. `--days 7` limits it to recent data, `--stage filter` includes everything the
Filter saw. It describes past memecoins, not future ones: use it to choose what to paper-test next.

### Market regime (measurement only)
With `market_regime = true` (in `[quant]`) the desk reads BTC (cbBTC), SOL and a basket of large Solana
memecoins (BONK, WIF, POPCAT, FARTCOIN, TRUMP, PNUT) from Jupiter's price API once a minute, plus the
median 1h move and volume of the new tokens the Filter is screening. That snapshot is stored with every
shadow row and every real trade. `python run.py research` then adds a "By market regime" section:
"crash hours" (BTC 1h <= -2%, SOL 1h <= -3% or meme index 1h <= -5%) against normal hours, and outcomes
per bucket of each regime value. Nothing blocks entries on it: an entry block is worth adding only if
this report shows a clear difference over several weeks of data.

## Controller — checks and configuration proposals (`python run.py controller`)
The Controller (Controllore) runs inside the desk with two read-only jobs:

* **Behaviour checks** every `health_seconds` (60): every agent is alive and making progress (Scout discovers,
  Filter screens, Analyst scores, Executor answers, Risk polls open positions); the hard rules hold on real
  positions and trades (max open positions, max 8% per trade at entry, score >= threshold for every buy,
  nothing left open under the hard stop, trailing armed after +12%); stops that filled far beyond the stop,
  a stalled 60-minute follow-up, queues piling up, HTTP 429s from an API, and a funnel where nothing passes
  (with the most common reason). Problems appear in the feed once, and in the dashboard's Controllore section.
* **Tests** every `interval_minutes` (10): the shadow tracker now also stores each scored token's price
  path. On those real tokens the Controller replays other settings per agent: Scout sources, stricter Filter
  thresholds, Analyst threshold and component weights (tokens re-scored offline), Risk stop / trailing /
  time stop (on the price paths), and what the trades the Executor declined did afterwards. Each round tries
  the current setting, one-change variants, new random combinations and the best of earlier rounds, split by
  situation (market crash / normal, memecoin index up / flat / down, young / mature token, small / large pool).
  The winner is chosen on the older 70% of the data and must also win on the newest 30% (paired comparison,
  95% interval; 99% for single situations). Proposals never cross the hard limits. Stops wider than -6%
  (down to -30%, with wider trailing and time exits) are also replayed, as a measure only: they get their own
  report section ("Stop più larghi del -6%") and are never proposed, so changing the rule stays your call.

The report is in `data/reports/controller_<mode>_latest.md`, in the dashboard (section Controllore, also
shown in the 3D view) and on demand with `python run.py controller`. All of it uses local data only, so it
costs no API budget.

**Applying by itself** (`auto_apply = "paper"`, the default: paper desk only; `"always"` also live, `"off"` proposals
only). A SOLIDA result for all situations is applied at once, a PROMETTENTE one after `confirm_rounds` (3) rounds
in a row; at most one change per agent per `apply_cooldown_minutes` (60); never beyond the hard limits, never all
Scout sources off, never a change the data already disproved. Each decision is stored forever in the
`controller_decisions` table with the values before/after, the numbers behind it and the reasoning in words.
After `review_min_rows` (30) new tokens the change is re-checked against the old setting on those tokens: it is
marked *confermata*, or *smentita* and rolled back automatically (the roll-back is a decision too). Applied values
are kept in `data/controller_overrides_<mode>.json` and loaded over `config.toml` at every start; delete that file
to return to `config.toml`. The Controller's memory (rounds, best variants, streaks, decisions, last report)
survives restarts.


## Model — P(win) learned from the 60-minute follow-ups (`python run.py model`)

Every Controller round also retrains a small statistical model (`desk/model.py`: boosted depth-2 trees on
quantile bins, numpy, CPU, a few seconds) on the Analyst follow-ups. It estimates P(win), the chance that the
current exit rules close a token in profit after costs, from the ~35 numbers seen at decision time. It learns
on the oldest 70% of the data and is judged on the newest 30% it never saw; a block threshold is picked on the
training part only. It starts "in prova" (P(win) is logged in every buy reason, nothing is blocked) and blocks
buys under the threshold only after a SOLIDA verdict, or PROMETTENTE for `confirm_rounds` rounds in a row, where
`[controller] auto_apply` allows it. When the advantage disappears it switches the block off by itself. It can
only block, never force a buy or loosen a rule. Settings: `[model]` in `config.toml` (`min_rows` = follow-ups
needed before the first training). `python run.py model` (or `start.bat model`) counts the learning data and
runs one training round on it, read-only, with the bot running or not.

## Engine
* Filter runs in two stages: a batched market screen, then a priority queue of deep checks
  (graduations and smart-money signals first, then by volume) with `deep_workers` in parallel.
  Deep checks whose market data waited longer than `deep_max_wait_seconds` are dropped (the Scout resubmits).
* The Analyst scores `workers` tokens in parallel, and skips clearances already half-expired.
* Exits have their own loop and each exit runs as its own task: a stop is never queued behind an entry,
  and several stops hit at once are sold in parallel. Risk is asked again right before every buy.

## Execution — how positions are opened and closed
Live trading uses **Jupiter Swap V2** (`/swap/v2/order` + `/swap/v2/execute`, the successor of Ultra):
* **One call = quote + transaction.** The order that passes the impact / round-trip / EV checks is
  exactly the one that gets signed and sent; no rebuild in between.
* **Entries** run in "ultra" mode: Jupiter's real-time slippage estimate (RTSE), RFQ market makers
  competing with on-chain routes, MEV-protected landing, gasless when possible. If Jupiter's slippage
  estimate is above `slippage_bps` (3%) the entry is skipped: too volatile to enter safely.
* **Entry checks run in parallel:** balance→size→order, token decimals and fresh market data are fetched
  at the same time, then the sell-back (honeypot) quote.
* **Exits:** first try at Jupiter's own slippage estimate (best price). If the sell fails it is retried
  **immediately** at `exit_slippage_ladder_bps` (800 → 1500 → 3000 bps), re-reading the wallet, instead of
  waiting for Risk's next tick. If an earlier attempt actually landed, it is booked, not sold twice.
* **Fills are exact and per transaction** (Jupiter's reported amounts, or the transaction metadata),
  never wallet balance deltas: there is no swap lock, so an exit never waits for an entry to land.
* Every fill records its timing (`checks_ms`, `swap_ms`, `trigger_to_fill_ms`, route) in the trade
  log and decision record.
* `execution = "rpc"` keeps the classic path (`/swap/v1/swap` sent through `SOLANA_RPC_URL`) with
  `skipPreflight`, `maxRetries = 0` and our own rebroadcast every 2 s until it lands or the blockhash
  expires. Use it only with a fast private/staked RPC (Helius, Triton, QuickNode…); on the public RPC
  Jupiter V2 lands faster.
Speed only reduces slippage and late stops; it does not make a trade profitable.

## GMGN wallet intelligence (optional, `GMGN_API_KEY`)
Read-only use of GMGN's OpenAPI (paid key, Plus plan or better; never its custodial swap routes):
* **Filter:** honeypot flag, GMGN rug label, insider / bundler / sniper shares, wash trading,
  serial-launcher dev (≥ 10 launches, ≤ 5% graduated), dev's SOL funder already in our rug ledger, and
  **funding clusters** (≥ 3 top holders funded by the same wallet = one hidden whale; also detects
  scripted funding within 60 s).
* **Analyst:** smart-money / KOL wallet counts (penalised for fresh-wallet, bot and rat-trader crowds).
* **Scout:** smart-money and KOL buy signals as a discovery source, tracked by source in the recap.
Missing GMGN data is treated as *unknown*, never as safe; on a 429 every GMGN call stops until the reset.

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
| Open the web dashboard | on your PC: `ssh -L 8080:127.0.0.1:8080 root@<server>` then browse http://127.0.0.1:8080 |
| Watch the live terminal | `docker attach phantom-desk` — leave with **Ctrl-P Ctrl-Q** (Ctrl-C stops the desk) |
| Get the 4h recap on your phone | put `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (or `DISCORD_WEBHOOK_URL`) in `/opt/phantom-desk/.env`, then `docker compose up -d` |
| Read recaps on the server | `ls /opt/phantom-desk/data/reports` |
| Recap right now | `docker exec phantom-desk python run.py recap` |
| Is live unlocked? | `docker exec phantom-desk python run.py gate` |
| Go live (after the gate passes) | set `mode = "live"` in `config.toml`, fill the LIVE section of `.env`, `docker compose up -d` |

### Run it on your own PC (Windows)
1. Install **Python 3.12** from python.org (tick **"Add python.exe to PATH"**).
2. Get the code (the GitHub "Code → Download ZIP" button works too, then unzip).
3. **Double-click `start.bat`.** The first run creates `.venv`, installs everything (1–2 min) and creates
   `.env`; then the dashboard opens. Every later run starts straight away.
   * `start.bat recap` · `start.bat gate` · `start.bat wallet` · `start.bat --headless` from a terminal.
   * Ctrl-C stops the desk; open positions are saved and resume on the next start.
4. **Web dashboard:** your browser opens **http://127.0.0.1:8080** by itself (balance, equity curve, the six
   agents live, positions, P&L per trade, every decision with its reason, QUANT calibration, recap on demand).
   It's read-only and reachable only from your own PC. The terminal dashboard keeps working alongside it.
   Right below the account value, the **Vista 3D** section shows the six agents in 3D:
   every operation is a block on a spiral around its agent (click it for the comment, score breakdown and the
   token's full path), and tokens fly between agents as they move through the pipeline. The 3D library (three.js)
   loads from a CDN, so that section needs internet; the rest of the dashboard doesn't.
5. The desk only runs while the PC is on and awake (disable sleep, or use the VPS setup for 24/7).

### See the dashboard from anywhere (GitHub Pages, read-only)
The desk can publish a **public, read-only copy** of the dashboard on GitHub Pages while it keeps running on
your PC. Every 10 minutes it writes a snapshot (balance, positions, trades, agents, 3D activity, Controller
report, recap) and force-pushes it as a single commit to the `gh-pages` branch; the page reads those files
instead of the local API. It runs in the background: git runs in a separate thread with a timeout and every
error is only logged, so publishing can't slow or stop trading. Nothing on the page can send commands.
1. The folder must be a **git clone** with push rights (Download ZIP won't work), e.g. the one you already `git pull`.
2. Publish once: start the desk, then in a second terminal `start.bat pages` (or `python run.py pages`).
3. On GitHub: **Settings → Pages → Build and deployment → Deploy from a branch → `gh-pages` / `(root)`**, Save.
   After ~1 minute the page is at `https://<user>.github.io/<repo>/`.
4. It stays updated automatically: `[pages] enabled = true` is the default in `config.toml` (set it to `false` to stop publishing).

**The page is public** (anyone with the link; search engines are asked not to index it): balances, trades and
token names are visible. In live mode nothing is published unless `publish_live = true`, because live trades can
be traced to your wallet on-chain. The page shows how old the data is and turns red if the desk stops updating it.

Linux/macOS: `./start.sh` (same commands: `./start.sh recap`, `gate`, `wallet`, `--headless`).

## Connecting a Phantom wallet (live only — paper needs no wallet)
The desk doesn't "connect" like a website does: it signs swaps itself with the wallet's private key, which
stays on your machine in `.env` and is never sent anywhere (Jupiter only receives the *signed* transaction).
1. In Phantom, create a **new, dedicated account** for the bot (Phantom → account menu → *Add / Connect
   Wallet* → *Create new account*). Fund it only with what you accept to risk. Never use your main wallet.
2. Export its key: Phantom → ⚙ *Settings* → *Manage Accounts* → the bot account → *Show Private Key* →
   password → copy the long base58 string.
3. Paste it in `.env`: `WALLET_PRIVATE_KEY=<that string>` (no quotes, no spaces). Never share it, never
   commit `.env` (it's git-ignored), never paste it in a chat.
4. Check it: `start.bat wallet` (or `python run.py wallet`) shows the address, balance and whether live is
   unlocked, without ever printing the key.
5. Go live only after the paper gate passes: set `mode = "live"` in `config.toml` and
   `LIVE_CONFIRM=I_ACCEPT_THE_RISK` in `.env`, then start again. A private RPC (`SOLANA_RPC_URL`, e.g.
   Helius) is strongly recommended for live.

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

## Third-party repos evaluated
| Repo | Verdict |
|---|---|
| GMGNAI/gmgn-skills | **Integrated** (read-only data API + holder-cluster and serial-dev logic, re-implemented in Python) |
| TauricResearch/TradingAgents | Inspiration only: stock-focused, minutes per decision; too slow and costly for memecoin entries |
| sendaifun/solana-agent-kit | Inspiration only: TypeScript; its RugCheck/DexScreener/Jupiter parts duplicate ours (and use the old Jupiter v6 API) |
| 0xsyncroot/opentrade | Rejected: wraps the same GMGN API with looser safety gates, trades through GMGN's custodial wallet |
| 8bitsats/Solana-MCP-Trading-Server | Rejected: no code, and its docs contain **exposed secrets including a Solana private key**: never use that wallet |

## Tests
`python -m pytest -q` covers the mandate: config can't loosen limits, sizing can never exceed
8%, the −6% stop, trail only after +12%, no averaging down / revenge trades, optional losing-streak pause (off by default),
the Executor refusing forged, tampered, expired or partial clearances, QUANT (warm-up, blocking
negative EV, cost subtraction, calibration), shadow barrier labelling, and the GMGN parsing, kills and
rate-limit pause.
