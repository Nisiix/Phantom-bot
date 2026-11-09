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

# ── Non-negotiable rules (from the desk mandate) ─────────────────────────────
HARD_MAX_POSITION_PCT = 8.0      # never more than 8% of current balance per trade
HARD_MIN_SCORE = 0.6             # Analyst score under 0.6 = no trade
HARD_STOP_PCT = -6.0             # hard stop at -6%
HARD_TRAIL_ACTIVATE_PCT = 12.0   # trail winners only after +12%
HARD_MAX_TOP10_PCT = 20.0
HARD_MIN_VOLUME_H1_USD = 50_000.0

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ScoutCfg:
    pumpportal: bool = True
    geckoterminal: bool = True
    dexscreener: bool = True
    watch_minutes: float = 180
    recheck_seconds: float = 45
    max_watchlist: int = 4000


@dataclass
class FilterCfg:
    require_mint_revoked: bool = True
    min_lp_locked_pct: float = 95.0
    max_top10_pct: float = 20.0
    min_volume_h1_usd: float = 50_000.0
    reject_rug_linked_dev: bool = True
    require_freeze_revoked: bool = True
    reject_dangerous_token2022: bool = True
    reject_rugcheck_danger: bool = True
    min_liquidity_usd: float = 20_000.0
    serial_dev_min_launches: int = 3
    serial_dev_dead_ratio: float = 0.8


@dataclass
class AnalystCfg:
    min_score: float = 0.6
    website_check: bool = True
    claude_website_review: bool = True
    claude_model: str = "claude-opus-5-5"


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


@dataclass
class RiskCfg:
    hard_stop_pct: float = -6.0
    trail_activate_pct: float = 12.0
    trail_distance_pct: float = 6.0
    cooldown_after_loss_minutes: float = 20
    max_consecutive_losses: int = 3
    streak_pause_minutes: float = 90
    daily_loss_limit_pct: float = 10.0
    liquidity_drop_exit_pct: float = 35.0
    max_hold_minutes: float = 0
    price_poll_seconds: float = 2.0


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


@dataclass
class Config:
    mode: str = "paper"
    paper_starting_balance_sol: float = 10.0
    data_dir: Path = ROOT / "data"
    scout: ScoutCfg = field(default_factory=ScoutCfg)
    filter: FilterCfg = field(default_factory=FilterCfg)
    analyst: AnalystCfg = field(default_factory=AnalystCfg)
    executor: ExecutorCfg = field(default_factory=ExecutorCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    recap: RecapCfg = field(default_factory=RecapCfg)
    live_gate: LiveGateCfg = field(default_factory=LiveGateCfg)
    secrets: Secrets = field(default_factory=Secrets)
    warnings: list[str] = field(default_factory=list)

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
    clamp(r, "trail_activate_pct", lambda v: v >= HARD_TRAIL_ACTIVATE_PCT, HARD_TRAIL_ACTIVATE_PCT,
          "trailing may only start after +12%")
    clamp(r, "trail_distance_pct", lambda v: 0 < v < HARD_TRAIL_ACTIVATE_PCT, 6.0,
          "trail distance must be between 0 and 12%")
    clamp(f, "max_top10_pct", lambda v: 0 < v <= HARD_MAX_TOP10_PCT, HARD_MAX_TOP10_PCT,
          "top-10 holder ceiling can never exceed 20%")
    clamp(f, "min_volume_h1_usd", lambda v: v >= HARD_MIN_VOLUME_H1_USD, HARD_MIN_VOLUME_H1_USD,
          "1h volume floor can never be below $50k")
    for flag in ("require_mint_revoked", "reject_rug_linked_dev"):
        clamp(f, flag, lambda v: v is True, True, f"{flag} is mandatory")
    clamp(f, "min_lp_locked_pct", lambda v: 50.0 <= v <= 100.0, 95.0, "LP lock threshold must be 50-100%")
    if cfg.mode not in ("paper", "live"):
        w.append(f"config: mode {cfg.mode!r} invalid, using 'paper'")
        cfg.mode = "paper"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
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
        raw = tomllib.loads(path.read_text())
    cfg = Config()
    w = cfg.warnings
    cfg.mode = str(raw.get("mode", cfg.mode)).lower()
    cfg.paper_starting_balance_sol = float(raw.get("paper_starting_balance_sol", cfg.paper_starting_balance_sol))
    dd = Path(raw.get("data_dir", "data"))
    cfg.data_dir = dd if dd.is_absolute() else ROOT / dd
    for section in ("scout", "filter", "analyst", "executor", "risk", "recap", "live_gate"):
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

    enforce_hard_limits(cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
