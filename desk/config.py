"""Configuration loading.

The user's trading rules are encoded here as HARD LIMITS. Values from
config.toml may only make them stricter; anything looser is clamped back and a
warning is recorded so the terminal can show it.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .pools import DEFAULT_QUOTES

# ── Non-negotiable rules (from the desk mandate) ─────────────────────────────
HARD_MAX_POSITION_PCT = 8.0      # never more than 8% of current balance per trade
HARD_MIN_SCORE = 0.6             # Analyst score under 0.6 = no trade
HARD_STOP_PCT = -6.0             # hard stop at -6%
NO_STOP_PCT = -100.0             # loss_stop = false: the stop sits at -100%, i.e. it never sells at a loss
PAPER_STOP_FLOOR_PCT = -30.0     # paper-only trial of wider stops (user's choice 2026-11-11): never wider than this
HARD_TRAIL_ACTIVATE_PCT = 12.0   # trail winners only after +12%
HARD_MAX_TOP10_PCT = 20.0
HARD_MIN_VOLUME_H1_USD = 50_000.0

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ScoutCfg:
    pumpportal: bool = True
    geckoterminal: bool = True
    dexscreener: bool = True
    venues: str = "pumpfun"         # "pumpfun" = pump.fun + PumpSwap only, "all" = also Raydium / LaunchLab / others
    watch_minutes: float = 180
    recheck_seconds: float = 45
    max_watchlist: int = 4000
    # Second look ("rianalisi"): a token dropped from the watchlist (watch window over, or gone quiet) is not
    # forgotten. It goes to an archive that is re-read every revisit_seconds for revisit_hours after discovery;
    # if it trades again (volume and liquidity above the bars below) it goes back on the watchlist and through
    # the whole Filter -> Analyst -> Risk chain again. Permanent kills (blacklist) never come back.
    revisit: bool = True
    revisit_hours: float = 24.0
    revisit_seconds: float = 300.0
    revisit_min_volume_usd: float = 10_000.0     # last-hour volume needed to bring a token back
    revisit_min_liquidity_usd: float = 10_000.0
    revisit_max: int = 5000                      # archive size cap (oldest dropped first)


@dataclass
class FilterCfg:
    require_mint_revoked: bool = True
    min_lp_locked_pct: float = 95.0
    max_unlockable_liquidity_pct: float = 50.0   # max share of liquidity in DLMM/CLMM pools (no LP token to lock)
    onchain_lp_check: bool = True       # read LP locks / concentrated-pool owners from the chain (desk/onchain_lp.py)
    max_top10_pct: float = 20.0
    min_volume_h1_usd: float = 50_000.0
    # Tokens younger than [analyst] young_age_hours: the volume floor follows the pool instead of a fixed $50k/h.
    # floor = young_volume_liquidity_ratio x liquidity (capped at min_volume_h1_usd), scaled by the pace in the
    # first hour (a 15-minute-old token can't have an hour of volume), never under young_min_volume_usd.
    young_volume_liquidity_ratio: float = 0.5
    young_min_volume_usd: float = 5_000.0
    # New tokens only (user 2026-11-11): 0 = off. Token older than max_age_hours (since its oldest DEX pair), or
    # with a market cap already above max_market_cap_usd, is not considered.
    max_age_hours: float = 0.0
    max_market_cap_usd: float = 0.0
    reject_rug_linked_dev: bool = True
    require_freeze_revoked: bool = True
    reject_dangerous_token2022: bool = True
    reject_rugcheck_danger: bool = True
    min_liquidity_usd: float = 20_000.0
    # The main pool must trade against one of these (default: SOL only, user rule 2026-11-12). Empty list = off.
    allowed_quote_mints: list = field(default_factory=lambda: list(DEFAULT_QUOTES))
    serial_dev_min_launches: int = 3
    serial_dev_dead_ratio: float = 0.8
    # Who funded the creator (user 2026-11-12, desk/funding.py): block a buy when the creator's funder (up to
    # funder_hops wallets up) is in the rug ledger or funded another wallet that is. Hubs (exchanges: >=
    # funder_hub_signatures transactions in the last hour) are never used for a link.
    funder_check: bool = True
    funder_hops: int = 2
    funder_hub_signatures: int = 1000
    funder_backfill_seconds: float = 4.0   # one ledger wallet looked up every N s in the background (0 = off)
    deep_workers: int = 3               # parallel deep checks (RugCheck keeps its own rate limit)
    deep_max_wait_seconds: float = 60   # drop queued deep checks whose market data got stale
    # Instant-rug guards (2026-11-10: 16 of 32 paper trades lost 60-100%, all bought 5-26 min after graduation).
    graduation_wait_minutes: float = 45.0   # no buy while the PumpSwap pair is younger than this (0 = off)
    # Early release of the wait for a token steady since graduation (see Filter._early_release).
    early_release: bool = True
    early_release_min_minutes: float = 20.0
    early_release_first_seen_minutes: float = 15.0
    early_release_min_readings: int = 3
    early_release_min_span_minutes: float = 10.0
    early_release_max_dev_pct: float = 5.0
    spread_top10_high_pct: float = 70.0     # top10 seen above this...
    spread_window_minutes: float = 60.0     # ...within this window and now under max_top10_pct = supply spread
                                            # over fake wallets -> permanent kill
    rugged_name_days: float = 7.0           # relaunches of a name/symbol that rugged in the last N days are killed
    rugged_name_loss_pct: float = 60.0      # a trade losing >= this within an hour counts as a rug of its name
    rugged_name_identity: bool = True       # same name as a rugged token: let a proven different project through
    identity_min_score: float = 3.0         # points needed: CA on own site 2, DexScreener profile 1, own X 1, site links X 1
    identity_tg_min_members: int = 20       # linked Telegram group/channel that exists with >= N members: +1; never a minus (optional extra)


@dataclass
class AnalystCfg:
    min_score: float = 0.6
    website_check: bool = True
    claude_website_review: bool = True
    claude_model: str = "claude-opus-5-5"
    # Score weights (keys of desk/agents/analyst.py WEIGHTS). Missing keys keep the built-in weight; the set is
    # renormalised to sum 1. Empty = built-in weights. The Controller proposes changes here.
    weights: dict = field(default_factory=dict)
    workers: int = 3                    # tokens scored in parallel (a slow website no longer blocks the rest)
    # Age-aware caution. For a token younger than young_age_hours DexScreener's "6h" change is its whole life
    # (since launch / graduation), so it is not a sign of a late entry: only the parabolic cap applies.
    # Older tokens may have pumped already: stricter caps.
    young_age_hours: float = 4.0
    old_late_entry_h6_pct: float = 300.0   # older token up more than this in 6h -> capped (young: no 6h cap)
    old_parabolic_h1_pct: float = 200.0    # older token up more than this in 1h -> capped (young: 400%)


@dataclass
class ExecutorCfg:
    max_position_pct: float = 8.0
    min_trade_sol: float = 0.05
    max_open_positions: int = 4
    slippage_bps: int = 300
    max_entry_price_impact_pct: float = 2.5
    max_round_trip_loss_pct: float = 6.0
    clearance_ttl_seconds: float = 120
    fee_reserve_sol: float = 0.05
    priority_fee_max_lamports: int = 200_000
    execution: str = "jupiter_v2"        # live: "jupiter_v2" (order/execute) or "rpc" (own RPC landing)
    exit_slippage_ladder_bps: list = field(default_factory=lambda: [800, 1500, 3000])


@dataclass
class EntryCfg:
    # When to buy an approved token (desk/entry.py). "now" = at once; "dip" / "bounce" / "confirm" = wait up to
    # window_minutes for a better moment. The Controller tests these rules and switches when one is confirmed.
    mode: str = "now"
    window_minutes: float = 10.0
    dip_pct: float = 5.0
    bounce_pct: float = 2.0
    confirm_minutes: float = 5.0
    confirm_pct: float = 0.0
    fallback: str = "skip"
    max_liq_drop_pct: float = 20.0
    poll_seconds: float = 6.0
    # User's trial (UTC ISO time, "" = none): the rule above was chosen by the user. While set, the Controller
    # does not change [entry] (its own overrides for it are ignored) and measures the rule against "buy at once"
    # on the tokens approved since then; when the newer data disproves it, it flags it as "da rimuovere".
    trial_since: str = ""


@dataclass
class RiskCfg:
    hard_stop_pct: float = -6.0
    # Paper-only trial of a stop wider than the -6% hard limit (user's choice 2026-11-11, from the Controller's
    # "Stop più larghi" measure). 0 = off. Ignored in live: there the -6% hard limit always applies.
    paper_hard_stop_pct: float = 0.0
    # The stop Risk actually exits at (set on every load by enforce_hard_limits, not a config.toml key):
    # paper_hard_stop_pct in paper when set, else hard_stop_pct. QUANT labels keep using hard_stop_pct.
    exit_stop_pct: float = 0.0
    # User's rule 2026-11-11 (paper AND live): no stop loss while a position is at break-even or at a loss. false =
    # no hard stop (exit_stop_pct becomes NO_STOP_PCT) and no time exit (max_hold_minutes forced to 0); the profit
    # stops (profit lock, rising stop, trailing) and the rug exits (liquidity pull, data loss) stay. true = old rules.
    loss_stop: bool = True
    trail_activate_pct: float = 12.0
    trail_distance_pct: float = 6.0
    cooldown_after_loss_minutes: float = 20
    max_consecutive_losses: int = 3
    streak_pause_minutes: float = 0
    daily_loss_limit_pct: float = 30.0
    liquidity_drop_exit_pct: float = 35.0
    data_loss_exit_seconds: float = 60.0   # emergency exit after this long with no price and no sell quote (0 = off)
    max_hold_minutes: float = 0
    price_poll_seconds: float = 2.0
    hot_poll_seconds: float = 1.0          # ...while a position is pumping (min 1 s)
    # PROFIT LOCK (user's rule 2026-11-11, see desk/exits.py): once the trade is up profit_lock_activate_pct
    # NET (or profit_lock_fast_activate_pct if it got there within profit_lock_fast_minutes), the stop moves to
    # profit_lock_pct NET, so the trade still ends in profit. All levels are net of costs and are raised by the
    # sell buffer (sale fees/slippage; Risk uses its measured slippage when that is larger). 0 = off.
    profit_lock_activate_pct: float = 5.0
    profit_lock_fast_activate_pct: float = 6.0
    profit_lock_fast_minutes: float = 3.0
    profit_lock_pct: float = 2.5
    profit_lock_sell_buffer_pct: float = 1.0
    profit_lock_keep_pct: float = 50.0     # lock keeps this share of the net peak profit (0 = fixed lock)
    profit_lock_fee_multiple: float = 0.0  # arm only once net profit >= this x the trade's round-trip fees too
    # Stop that rises with the price (user 2026-11-11): from profit_lock_keep_from_pct net peak the stop keeps
    # profit_lock_keep_pct of the peak, up to profit_lock_keep_fast_pct when the peak rises profit_lock_speed_full
    # points per minute or more (measured over profit_lock_speed_seconds). 0 = each part off.
    profit_lock_keep_from_pct: float = 0.0
    profit_lock_keep_fast_pct: float = 0.0
    profit_lock_speed_full: float = 0.0
    profit_lock_speed_seconds: float = 60.0
    # Wider stop on mid peaks (user 2026-11-12): under profit_lock_keep_high_from_pct net peak the stop keeps
    # profit_lock_keep_low_pct of the peak instead of profit_lock_keep_pct. 0 = off.
    profit_lock_keep_low_pct: float = 0.0
    profit_lock_keep_high_from_pct: float = 0.0
    # Partial sale (user 2026-11-12): once the position is up scale_out_at_pct NET, sell scale_out_fraction_pct of
    # it once; the rest stays under the usual stops. 0 = off.
    scale_out_at_pct: float = 0.0
    scale_out_fraction_pct: float = 0.0


@dataclass
class QuantCfg:
    enabled: bool = True
    horizon_minutes: float = 60
    min_samples_to_enforce: int = 30
    min_ev_pct: float = 0.0
    prior_strength: float = 10.0
    shadow_tracking: bool = True
    shadow_weight: float = 0.5
    shadow_min_liquidity_usd: float = 5000.0
    default_cost_pct: float = 2.5
    # QUANT learns only from trades / follow-ups after this moment (UTC ISO time, "" = all). Default: when the
    # instant-rug protections went live (graduation wait, holder-spread kill, rugged names); earlier trades
    # describe tokens the bot no longer buys. The rows stay in the database.
    learn_since: str = "2026-11-10T09:03:00Z"
    market_regime: bool = True        # record BTC/SOL/meme regime with every shadow row & trade (no blocking)


@dataclass
class GmgnCfg:
    enabled: bool = True              # used only when GMGN_API_KEY is set
    plan: str = "plus"                # free | plus | pro  (rate limits)
    signal_feed: bool = True          # Scout: smart-money & KOL buy signals
    max_rug_ratio: float = 0.30       # GMGN rug label (0-1)          -> permanent kill
    max_insider_rate: float = 0.25    # suspected insider holdings     -> reject
    max_bundler_rate: float = 0.30    # bundler-held share             -> reject
    max_sniper_rate: float = 0.30     # top-70 sniper holdings         -> reject
    max_funding_cluster_pct: float = 15.0  # % supply in wallets funded by the same wallet -> reject
    serial_min_launches: int = 10     # dev with >= N launches...
    serial_max_graduation: float = 0.05    # ...and <= 5% graduated      -> permanent kill


@dataclass
class DevWatchCfg:
    """User's rule 2026-11-11 (desk/devwatch.py): the creator and its linked wallets must not sell more than
    max_sold_pct of what they held from the start. No position -> permanent kill; open position -> sell."""
    enabled: bool = True
    max_sold_pct: float = 10.0        # % of the group's starting holding
    min_hold_pct: float = 0.1         # a group that held less than this % of the supply is dust: not judged
    position_check_seconds: float = 20.0   # open positions: on-chain balances read this often (1 RPC call each)
    filter_check_seconds: float = 60.0     # no position: at most one on-chain read per token this often
    max_linked_wallets: int = 12      # wallets taken from the insider networks that contain the creator


@dataclass
class ControllerCfg:
    enabled: bool = True
    interval_minutes: float = 10        # a new round of tests (offline, on the desk's own data) every N minutes
    health_seconds: float = 60          # behaviour checks on the other agents every N seconds
    random_variants: int = 24           # new random parameter combinations tried per agent per round
    min_rows: int = 30                  # fewest tokens a situation needs before it is judged
    holdout_pct: float = 30.0           # newest share of the data kept aside to verify the winner
    lookback_days: float = 14           # only data from the last N days
    # BASE RULE (user, 2026-11-11): the Controller only PROPOSES, the user decides. Always "off" (clamped below);
    # "paper"/"always" (applying by itself) are refused. Old applied values: data/controller_overrides_<mode>.json.
    auto_apply: str = "off"
    confirm_rounds: int = 3             # a PROMETTENTE result must win this many rounds in a row (SOLIDA: at once)
    apply_cooldown_minutes: float = 60  # at most one change per agent in this time, so each effect can be measured
    review_min_rows: int = 30           # new tokens needed before an applied change is confirmed or rolled back


@dataclass
class ModelCfg:
    # MODELLO (desk/model.py): boosted trees trained by the Controller every round on the 60-min follow-ups.
    enabled: bool = True                # train it and log P(win) for every buy
    enforce: bool = False               # YOUR approval: true = may BLOCK buys once verified (SOLIDA, or PROMETTENTE
                                        # confirm_rounds times). false = the block is only proposed (rule 2026-11-11)
    min_rows: int = 1000                # fewest Analyst follow-ups with stored values before the first training
    min_leaf: int = 40                  # fewest tokens in a tree leaf (bigger = smoother, less overfitting)
    min_eval_rows: int = 30             # fewest tradeable tokens in the verification part to judge on them alone
    max_block_pct: float = 40.0         # never pick a threshold that would block more than this share of buys


@dataclass
class DashboardCfg:
    enabled: bool = True
    host: str = "127.0.0.1"          # localhost only; Docker sets DASHBOARD_HOST=0.0.0.0 inside the container
    port: int = 8080
    open_browser: bool = True


@dataclass
class PagesCfg:
    """Public read-only copy of the dashboard on GitHub Pages (see desk/pages.py)."""
    enabled: bool = False
    interval_minutes: float = 10        # GitHub Pages rebuilds at most ~10 times/hour: never below 6
    remote: str = ""                    # git remote to push to; empty = the "origin" of this checkout
    branch: str = "gh-pages"
    publish_live: bool = False          # in live mode nothing is published unless this is true


@dataclass
class RecapCfg:
    interval_hours: float = 4


@dataclass
class LiveGateCfg:
    min_paper_trades: int = 30
    require_positive_paper_pnl: bool = True
    max_paper_drawdown_pct: float = 20.0


@dataclass
class Secrets:
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    wallet_private_key: str = ""
    live_confirm: str = ""
    anthropic_api_key_present: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    gmgn_api_key: str = ""


@dataclass
class Config:
    mode: str = "paper"
    paper_starting_balance_sol: float = 10.0
    data_dir: Path = ROOT / "data"
    scout: ScoutCfg = field(default_factory=ScoutCfg)
    filter: FilterCfg = field(default_factory=FilterCfg)
    analyst: AnalystCfg = field(default_factory=AnalystCfg)
    executor: ExecutorCfg = field(default_factory=ExecutorCfg)
    entry: EntryCfg = field(default_factory=EntryCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    recap: RecapCfg = field(default_factory=RecapCfg)
    quant: QuantCfg = field(default_factory=QuantCfg)
    gmgn: GmgnCfg = field(default_factory=GmgnCfg)
    devwatch: DevWatchCfg = field(default_factory=DevWatchCfg)
    dashboard: DashboardCfg = field(default_factory=DashboardCfg)
    pages: PagesCfg = field(default_factory=PagesCfg)
    controller: ControllerCfg = field(default_factory=ControllerCfg)
    model: ModelCfg = field(default_factory=ModelCfg)
    live_gate: LiveGateCfg = field(default_factory=LiveGateCfg)
    secrets: Secrets = field(default_factory=Secrets)
    warnings: list[str] = field(default_factory=list)
    controller_overrides: dict = field(default_factory=dict)   # settings applied by the Controller (over config.toml)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def _fill(dc, data: dict, section: str, warnings: list[str]):
    known = {f.name: f for f in fields(dc)}
    for k, v in (data or {}).items():
        if k not in known:
            warnings.append(f"config: unknown key [{section}].{k} ignored")
            continue
        cur = getattr(dc, k)
        try:
            if isinstance(cur, bool):
                v = bool(v)
            elif isinstance(cur, int) and not isinstance(cur, bool):
                v = int(v)
            elif isinstance(cur, float):
                v = float(v)
            elif isinstance(cur, str):
                v = str(v)
        except (TypeError, ValueError):
            warnings.append(f"config: bad value for [{section}].{k}={v!r}, using default {cur!r}")
            continue
        setattr(dc, k, v)


def enforce_hard_limits(cfg: Config) -> None:
    """Clamp anything that would loosen the mandate. Called on every load."""
    w = cfg.warnings

    def clamp(obj, name, ok, fixed, msg):
        if not ok(getattr(obj, name)):
            w.append(f"HARD LIMIT: {msg} (config had {getattr(obj, name)!r}, using {fixed!r})")
            setattr(obj, name, fixed)

    e, a, r, f = cfg.executor, cfg.analyst, cfg.risk, cfg.filter
    clamp(e, "max_position_pct", lambda v: 0 < v <= HARD_MAX_POSITION_PCT, HARD_MAX_POSITION_PCT,
          "position size can never exceed 8% of balance")
    clamp(a, "min_score", lambda v: HARD_MIN_SCORE <= v <= 1.0, HARD_MIN_SCORE,
          "analyst threshold can never be below 0.6")
    clamp(r, "hard_stop_pct", lambda v: HARD_STOP_PCT <= v < 0, HARD_STOP_PCT,
          "hard stop can never be looser than -6%")
    clamp(r, "paper_hard_stop_pct", lambda v: v == 0 or PAPER_STOP_FLOOR_PCT <= v < 0, 0.0,
          "paper stop trial must be 0 (off) or between -30% and 0")
    r.exit_stop_pct = r.paper_hard_stop_pct if (cfg.mode == "paper" and r.paper_hard_stop_pct < 0) \
        else r.hard_stop_pct
    if not r.loss_stop:
        # User's rule 2026-11-11: no stop at a loss (paper and live). The time exit closes at whatever P&L the
        # token has after N minutes, i.e. mostly at a loss, so it goes too (the Controller can't turn it back on).
        r.exit_stop_pct = NO_STOP_PCT
        clamp(r, "max_hold_minutes", lambda v: not v, 0.0,
              "no time exit while loss_stop = false (it would sell at a loss)")
    clamp(r, "trail_activate_pct", lambda v: v >= HARD_TRAIL_ACTIVATE_PCT, HARD_TRAIL_ACTIVATE_PCT,
          "trailing may only start after +12%")
    clamp(r, "trail_distance_pct", lambda v: 0 <= v < HARD_TRAIL_ACTIVATE_PCT, 6.0,
          "trail distance must be between 0 (off) and 12%")
    # BASE RULE of the engine (user 2026-11-11): the stop in profit is never turned off, by anyone (the
    # Controller turned it off at 19:41 UTC that day: with no stop at a loss a trade that rose and fell back
    # would never have been sold).
    clamp(r, "profit_lock_activate_pct", lambda v: v > 0, 8.0,
          "the profit lock can never be turned off (base rule of the engine)")
    clamp(r, "profit_lock_activate_pct", lambda v: 2.0 <= v <= 50.0, 5.0,
          "profit lock must arm between +2% and +50%")
    if r.profit_lock_activate_pct > 0:
        clamp(r, "profit_lock_fast_activate_pct", lambda v: v == 0 or r.profit_lock_activate_pct <= v <= 50.0,
              r.profit_lock_activate_pct, "fast profit-lock threshold must be >= profit_lock_activate_pct")
        clamp(r, "profit_lock_pct", lambda v: 0.5 <= v <= r.profit_lock_activate_pct - 1.0,
              max(0.5, min(2.5, r.profit_lock_activate_pct - 1.0)),
              "profit lock must sell at least +0.5% net and at least 1 point under its arming level")
        clamp(r, "profit_lock_sell_buffer_pct", lambda v: 0.5 <= v <= 10.0, 1.0,
              "profit lock sell buffer must be 0.5-10% (it covers the sale's fees)")
        clamp(r, "profit_lock_keep_pct", lambda v: 0 <= v <= 90, 50.0, "profit lock keep share must be 0-90%")
        clamp(r, "profit_lock_fast_minutes", lambda v: 0 <= v <= 60, 3.0, "profit lock fast window must be 0-60 min")
        clamp(r, "profit_lock_fee_multiple", lambda v: 0 <= v <= 5, 1.0, "profit lock fee multiple must be 0-5")
        clamp(r, "profit_lock_keep_from_pct", lambda v: 0 <= v <= 500, 30.0, "rising stop must start at 0-500%")
        clamp(r, "profit_lock_keep_fast_pct", lambda v: v == 0 or r.profit_lock_keep_pct <= v <= 95, 0.0,
              "the share kept on a fast rise must be between profit_lock_keep_pct and 95% (0 = off)")
        clamp(r, "profit_lock_speed_full", lambda v: 0 <= v <= 1000, 30.0, "profit_lock_speed_full must be 0-1000")
        clamp(r, "profit_lock_speed_seconds", lambda v: 20 <= v <= 600, 60.0, "speed window must be 20-600 s")
        clamp(r, "profit_lock_keep_low_pct", lambda v: 0 <= v <= 90, 0.0, "mid-peak keep share must be 0-90%")
        clamp(r, "profit_lock_keep_high_from_pct", lambda v: 0 <= v <= 500, 0.0,
              "mid-peak band must end at 0-500%")
    clamp(r, "scale_out_at_pct", lambda v: v == 0 or 5 <= v <= 1000, 0.0, "partial sale must trigger at +5% net or more")
    clamp(r, "scale_out_fraction_pct", lambda v: 0 <= v <= 90, 0.0, "partial sale share must be 0-90%")
    clamp(r, "hot_poll_seconds", lambda v: 1.0 <= v <= 10.0, 1.0, "hot_poll_seconds must be 1-10 s")
    clamp(f, "max_top10_pct", lambda v: 0 < v <= HARD_MAX_TOP10_PCT, HARD_MAX_TOP10_PCT,
          "top-10 holder ceiling can never exceed 20%")
    clamp(f, "young_volume_liquidity_ratio", lambda v: 0.25 <= v <= 5.0, 0.5,
          "young-token volume floor must be 0.25-5x its liquidity")
    sc = cfg.scout
    clamp(sc, "revisit_seconds", lambda v: v >= 60.0, 300.0, "second-look sweep can't run more than once a minute")
    clamp(sc, "revisit_min_volume_usd", lambda v: v >= 2_000.0, 10_000.0, "second-look volume bar can't be below $2k/h")
    clamp(f, "young_min_volume_usd", lambda v: v >= 2_000.0, 5_000.0, "young-token volume floor can't be below $2k/h")
    clamp(a, "old_late_entry_h6_pct", lambda v: 0 < v <= 500.0, 300.0, "late-entry cap can never exceed +500% in 6h")
    clamp(a, "old_parabolic_h1_pct", lambda v: 0 < v <= 400.0, 200.0, "parabolic cap can never exceed +400% in 1h")
    clamp(a, "young_age_hours", lambda v: 0 <= v <= 6.0, 4.0, "a token counts as young for at most 6 hours")
    clamp(f, "min_volume_h1_usd", lambda v: v >= HARD_MIN_VOLUME_H1_USD, HARD_MIN_VOLUME_H1_USD,
          "1h volume floor can never be below $50k")
    for flag in ("require_mint_revoked", "reject_rug_linked_dev"):
        clamp(f, flag, lambda v: v is True, True, f"{flag} is mandatory")
    clamp(f, "min_lp_locked_pct", lambda v: 50.0 <= v <= 100.0, 95.0, "LP lock threshold must be 50-100%")
    clamp(f, "max_unlockable_liquidity_pct", lambda v: 0.0 <= v <= 50.0, 50.0,
          "liquidity in unlockable (DLMM/CLMM) pools can be at most 50% of the total")
    q = cfg.quant
    clamp(q, "min_ev_pct", lambda v: v >= 0, 0.0, "QUANT may only demand a positive edge (min_ev_pct >= 0)")
    clamp(q, "min_samples_to_enforce", lambda v: 10 <= v <= 500, 30, "QUANT warm-up must be 10-500 trades")
    clamp(q, "horizon_minutes", lambda v: 5 <= v <= 24 * 60, 60.0, "QUANT horizon must be 5-1440 min")
    clamp(q, "shadow_weight", lambda v: 0 <= v <= 1, 0.5, "shadow sample weight must be 0-1")
    clamp(q, "default_cost_pct", lambda v: v >= 0.5, 2.5, "assumed round-trip cost must be >= 0.5%")
    clamp(q, "prior_strength", lambda v: 1 <= v <= 200, 10.0, "QUANT prior strength must be 1-200")
    clamp(e, "execution", lambda v: v in ("jupiter_v2", "rpc"), "jupiter_v2",
          'executor.execution must be "jupiter_v2" or "rpc"')
    clamp(e, "exit_slippage_ladder_bps",
          lambda v: isinstance(v, list) and 0 < len(v) <= 6 and all(isinstance(b, int) and 0 < b <= 5000 for b in v),
          [800, 1500, 3000], "exit slippage ladder must be 1-6 values of 1-5000 bps")
    from .agents.analyst import WEIGHTS as _W
    aw = a.weights
    if not isinstance(aw, dict) or any(k not in _W or not isinstance(v, (int, float)) or v < 0 for k, v in aw.items()) \
            or (aw and sum({**_W, **aw}.values()) <= 0):
        w.append(f"config: bad [analyst] weights {aw!r}, using the built-in weights")
        a.weights = {}
    en = cfg.entry
    clamp(en, "mode", lambda v: v in ("now", "dip", "bounce", "confirm"), "now",
          'entry mode must be "now", "dip", "bounce" or "confirm"')
    clamp(en, "fallback", lambda v: v in ("skip", "buy"), "skip", 'entry fallback must be "skip" or "buy"')
    clamp(en, "window_minutes", lambda v: 1 <= v <= 15, 10.0, "entry window must be 1-15 min")
    clamp(en, "dip_pct", lambda v: 0.5 <= v <= 30, 5.0, "entry dip must be 0.5-30%")
    clamp(en, "bounce_pct", lambda v: 0.5 <= v <= 20, 2.0, "entry bounce must be 0.5-20%")
    clamp(en, "confirm_minutes", lambda v: 0.5 <= v <= en.window_minutes, min(5.0, en.window_minutes),
          "entry confirm time must be within the entry window")
    clamp(en, "confirm_pct", lambda v: -10 <= v <= 20, 0.0, "entry confirm level must be -10..+20%")
    clamp(en, "max_liq_drop_pct", lambda v: 5 <= v <= 50, 20.0, "entry liquidity cancel must be 5-50%")
    clamp(en, "poll_seconds", lambda v: 3 <= v <= 30, 6.0, "entry price poll must be 3-30 s")
    c = cfg.controller
    clamp(c, "interval_minutes", lambda v: 1 <= v <= 24 * 60, 10.0, "controller interval must be 1-1440 min")
    clamp(c, "health_seconds", lambda v: 10 <= v <= 3600, 60.0, "controller health checks every 10-3600 s")
    clamp(c, "random_variants", lambda v: 0 <= v <= 200, 24, "controller random variants must be 0-200")
    clamp(c, "min_rows", lambda v: 10 <= v <= 5000, 30, "controller min_rows must be 10-5000")
    clamp(c, "holdout_pct", lambda v: 10 <= v <= 50, 30.0, "controller holdout must be 10-50%")
    clamp(c, "auto_apply", lambda v: v == "off", "off",
          "the Controller never applies its decisions by itself: it only proposes, you decide (user rule 2026-11-11)")
    clamp(c, "confirm_rounds", lambda v: 1 <= v <= 50, 3, "controller confirm_rounds must be 1-50")
    clamp(c, "apply_cooldown_minutes", lambda v: 10 <= v <= 7 * 24 * 60, 60.0, "controller cooldown must be 10 min-7 days")
    clamp(c, "review_min_rows", lambda v: 10 <= v <= 5000, 30, "controller review_min_rows must be 10-5000")
    if cfg.mode not in ("paper", "live"):
        w.append(f"config: mode {cfg.mode!r} invalid, using 'paper'")
        cfg.mode = "paper"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def load_config(path: str | Path | None = None, mode_override: str | None = None) -> Config:
    path = Path(path or os.environ.get("DESK_CONFIG", ROOT / "config.toml"))
    _load_dotenv(ROOT / ".env")
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    cfg = Config()
    w = cfg.warnings
    cfg.mode = str(raw.get("mode", cfg.mode)).lower()
    cfg.paper_starting_balance_sol = float(raw.get("paper_starting_balance_sol", cfg.paper_starting_balance_sol))
    dd = Path(raw.get("data_dir", "data"))
    cfg.data_dir = dd if dd.is_absolute() else ROOT / dd
    for section in ("scout", "filter", "analyst", "executor", "entry", "risk", "recap", "quant", "gmgn", "devwatch", "dashboard", "pages", "controller", "model", "live_gate"):
        _fill(getattr(cfg, section), raw.get(section, {}), section, w)
    if mode_override:
        cfg.mode = mode_override.lower()

    s = cfg.secrets
    s.rpc_url = os.environ.get("SOLANA_RPC_URL", s.rpc_url) or s.rpc_url
    s.wallet_private_key = os.environ.get("WALLET_PRIVATE_KEY", "")
    s.live_confirm = os.environ.get("LIVE_CONFIRM", "")
    s.anthropic_api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    s.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    s.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    s.discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    s.gmgn_api_key = os.environ.get("GMGN_API_KEY", "")
    if os.environ.get("DASHBOARD_HOST"):
        cfg.dashboard.host = os.environ["DASHBOARD_HOST"]

    enforce_hard_limits(cfg)
    # What the Controller learned and applied (data/controller_overrides_<mode>.json), over config.toml, still
    # inside the hard limits. Loaded whatever auto_apply says (the Controller no longer applies anything, but the values
    # it applied before 2026-11-11 stay until the user decides on them).
    from .controller_memory import load_overrides
    cfg.controller_overrides = load_overrides(cfg)
    if cfg.controller_overrides:
        enforce_hard_limits(cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
