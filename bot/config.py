"""Configuration loading, normalization and validation.

Reads ``.env`` (via python-dotenv) into a typed :class:`Config` object.
All parsing is defensive: a malformed value never crashes the bot, it is
either normalised (e.g. markdown-wrapped URLs) or rejected with a clear
error listing every problem found.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = BASE_DIR / ".env"

SUPPORTED_EXCHANGES = ("mexc", "okx", "bitget", "gate", "weex", "binance", "bybit", "kucoin")
SUPPORTED_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h")
STRATEGY_KEYS = (
    "ema_pullback",
    "vwap_reversion",
    "break_retest",
    "order_block",
    "macd_momentum",
    "rsi_divergence",
)


class ConfigError(Exception):
    """Raised when the configuration is invalid."""


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "enabled"):
        return True
    if s in ("0", "false", "no", "off", "disabled", ""):
        return False
    return default


def _normalize_url(value: str) -> str:
    """Strip markdown link wrappers like ``[https://x](https://x)`` -> ``https://x``."""
    value = (value or "").strip()
    if not value:
        return ""
    m = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", value)
    if m:
        return m.group(2).strip().rstrip("/")
    if value.startswith("[") and value.endswith(")"):
        inner = re.search(r"\]\(([^)]+)\)$", value)
        if inner:
            return inner.group(1).strip().rstrip("/")
    return value.strip().rstrip("/")


def _parse_float_list(raw: str, sep: str = ",") -> list[float]:
    out: list[float] = []
    for part in str(raw or "").split(sep):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def _parse_time_windows(raw: str) -> list[tuple[str, str]]:
    """Parse ``"12:30-13:45,13:30-14:45"`` into validated UTC windows."""
    windows: list[tuple[str, str]] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", part)
        if not m:
            raise ConfigError(f"Bad news window '{part}' (expected HH:MM-HH:MM)")
        start, end = m.group(1), m.group(2)
        for t in (start, end):
            hh, mm = t.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ConfigError(f"Bad news window time '{t}' (expected HH:MM)")
        windows.append((start, end))
    return windows


def _parse_strategy_flags(env: dict[str, Any]) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for key in STRATEGY_KEYS:
        flags[key] = _to_bool(env.get(f"STRATEGY_{key.upper()}"), True)
    return flags


@dataclass
class Config:
    # --- Telegram ---
    telegram_token: str = ""
    channel_id: str = ""
    admin_chat_id: str | None = None

    # --- Exchange ---
    primary_exchange: str = "mexc"
    enabled_exchanges: list[str] = field(default_factory=lambda: ["mexc"])
    gold_symbol: str = "XAU/USDT:USDT"
    mexc_api_key: str = ""
    mexc_api_secret: str = ""

    # --- AI ---
    openrouter_keys: list[str] = field(default_factory=list)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    ai_model: str = ""

    # --- Runtime toggles ---
    gold_enabled: bool = True
    meme_enabled: bool = False
    charts_enabled: bool = True
    ai_enabled: bool = True
    broadcast_enabled: bool = True
    tracking_enabled: bool = True
    error_notifications: bool = True

    # --- Scalping engine ---
    timeframes: list[str] = field(default_factory=lambda: ["1m", "5m", "15m"])
    poll_interval_seconds: float = 15.0
    signal_cooldown_minutes: float = 10.0
    min_signal_score: int = 65
    min_confluence_strategies: int = 2

    ema_fast: int = 9
    ema_mid: int = 21
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    stoch_k: int = 14
    stoch_d: int = 3
    donchian_period: int = 20
    volume_sma_period: int = 20
    vwap_dev_atr: float = 1.8

    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 2.5
    max_candles: int = 500

    strategy_enabled: dict[str, bool] = field(default_factory=dict)

    news_blackout_enabled: bool = True
    news_blackout_windows: list[tuple[str, str]] = field(
        default_factory=lambda: [("12:30", "13:45"), ("13:30", "14:45"), ("18:00", "19:30")]
    )
    weekend_filter: bool = False

    log_level: str = "INFO"
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, env_path: Path | str | None = None) -> "Config":
        env_path = Path(env_path) if env_path else DEFAULT_ENV_PATH
        if env_path.exists():
            load_dotenv(env_path, override=False)
        else:
            raise ConfigError(f".env file not found at {env_path}. Copy .env.example to .env and fill it in.")

        import os

        env = dict(os.environ)
        errors: list[str] = []

        def need(key: str) -> str:
            val = (env.get(key) or "").strip()
            if not val:
                errors.append(f"Missing required env var: {key}")
            return val

        token = need("TELEGRAM_TOKEN")
        channel = need("CHANNEL_ID")

        # Gold-specific fallbacks
        if not token:
            token = (env.get("TELEGRAM_GOLD_BOT_TOKEN") or "").strip()
        if not channel:
            channel = (env.get("TELEGRAM_GOLD_CHANNEL_ID") or "").strip()

        # Timeframes
        tfs = [t.strip() for t in env.get("TIMEFRAMES", "1m,5m,15m").split(",") if t.strip()]
        bad_tfs = [t for t in tfs if t not in SUPPORTED_TIMEFRAMES]
        if bad_tfs:
            errors.append(f"Unsupported timeframe(s): {', '.join(bad_tfs)} (supported: {', '.join(SUPPORTED_TIMEFRAMES)})")

        # Exchanges
        enabled_ex = [e.strip().lower() for e in env.get("ENABLED_EXCHANGES", "mexc").split(",") if e.strip()]
        primary = (env.get("PRIMARY_EXCHANGE") or "mexc").strip().lower()
        for e in enabled_ex:
            if e not in SUPPORTED_EXCHANGES:
                errors.append(f"Unsupported exchange '{e}' (supported: {', '.join(SUPPORTED_EXCHANGES)})")
        if primary not in enabled_ex:
            if primary in SUPPORTED_EXCHANGES:
                enabled_ex.insert(0, primary)
            else:
                errors.append(f"PRIMARY_EXCHANGE '{primary}' is not supported")

        # Numeric validation helper
        def num(key: str, cast: type, default, lo=None, hi=None):
            raw = (env.get(key) or "").strip()
            if raw == "":
                return default
            try:
                val = cast(raw)
            except (ValueError, TypeError):
                errors.append(f"{key} is not a number: '{raw}'")
                return default
            if lo is not None and val < lo:
                errors.append(f"{key} must be >= {lo} (got {val})")
            if hi is not None and val > hi:
                errors.append(f"{key} must be <= {hi} (got {val})")
            return val

        # News windows
        windows: list[tuple[str, str]] = []
        try:
            windows = _parse_time_windows(env.get("NEWS_BLACKOUT_WINDOWS", "12:30-13:45,13:30-14:45,18:00-19:30"))
        except ConfigError as exc:
            errors.append(str(exc))

        strategy_flags = _parse_strategy_flags(env)

        cfg = cls(
            telegram_token=token,
            channel_id=channel,
            admin_chat_id=(env.get("ADMIN_CHAT_ID") or "").strip() or None,
            primary_exchange=primary,
            enabled_exchanges=enabled_ex,
            gold_symbol=(env.get("GOLD_SYMBOL") or "XAU/USDT:USDT").strip(),
            mexc_api_key=(env.get("MEXC_API_KEY") or "").strip(),
            mexc_api_secret=(env.get("MEXC_API_SECRET") or "").strip(),
            openrouter_keys=[
                k for k in (
                    (env.get("OPENROUTER_API_KEY") or "").strip(),
                    (env.get("OPENROUTER_API_KEY_1") or "").strip(),
                    (env.get("OPENAI_API_KEY") or "").strip(),
                ) if k
            ],
            openrouter_base_url=_normalize_url(env.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"),
            ai_model=(env.get("AI_MODEL") or "").strip(),
            gold_enabled=_to_bool(env.get("GOLD_ENABLED"), True),
            meme_enabled=_to_bool(env.get("MEME_ENABLED"), False),
            charts_enabled=_to_bool(env.get("CHARTS_ENABLED"), True),
            ai_enabled=_to_bool(env.get("AI_ENABLED"), True),
            broadcast_enabled=_to_bool(env.get("BROADCAST_ENABLED"), True),
            tracking_enabled=_to_bool(env.get("TRACKING_ENABLED"), True),
            error_notifications=_to_bool(env.get("ERROR_NOTIFICATIONS"), True),
            timeframes=tfs or ["1m", "5m", "15m"],
            poll_interval_seconds=float(num("POLL_INTERVAL_SECONDS", float, 15.0, lo=3, hi=300)),
            signal_cooldown_minutes=float(num("SIGNAL_COOLDOWN_MINUTES", float, 10.0, lo=0, hi=1440)),
            min_signal_score=int(num("MIN_SIGNAL_SCORE", int, 65, lo=10, hi=100)),
            min_confluence_strategies=int(num("MIN_CONFLUENCE_STRATEGIES", int, 2, lo=1, hi=6)),
            ema_fast=int(num("EMA_FAST", int, 9, lo=2, hi=200)),
            ema_mid=int(num("EMA_MID", int, 21, lo=2, hi=200)),
            ema_slow=int(num("EMA_SLOW", int, 50, lo=2, hi=400)),
            rsi_period=int(num("RSI_PERIOD", int, 14, lo=2, hi=100)),
            atr_period=int(num("ATR_PERIOD", int, 14, lo=2, hi=100)),
            bb_period=int(num("BB_PERIOD", int, 20, lo=2, hi=200)),
            bb_std=float(num("BB_STD", float, 2.0, lo=0.1, hi=5.0)),
            macd_fast=int(num("MACD_FAST", int, 12, lo=2, hi=100)),
            macd_slow=int(num("MACD_SLOW", int, 26, lo=2, hi=200)),
            macd_signal=int(num("MACD_SIGNAL", int, 9, lo=2, hi=100)),
            stoch_k=int(num("STOCH_K", int, 14, lo=2, hi=100)),
            stoch_d=int(num("STOCH_D", int, 3, lo=1, hi=50)),
            donchian_period=int(num("DONCHIAN_PERIOD", int, 20, lo=5, hi=200)),
            volume_sma_period=int(num("VOLUME_SMA_PERIOD", int, 20, lo=2, hi=200)),
            vwap_dev_atr=float(num("VWAP_DEV_ATR", float, 1.8, lo=0.1, hi=10.0)),
            sl_atr_mult=float(num("SL_ATR_MULT", float, 1.5, lo=0.1, hi=10.0)),
            tp_atr_mult=float(num("TP_ATR_MULT", float, 2.5, lo=0.1, hi=20.0)),
            max_candles=int(num("MAX_CANDLES", int, 500, lo=120, hi=2000)),
            strategy_enabled=strategy_flags,
            news_blackout_enabled=_to_bool(env.get("NEWS_BLACKOUT_ENABLED"), True),
            news_blackout_windows=windows,
            weekend_filter=_to_bool(env.get("WEEKEND_FILTER"), False),
            log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
            data_dir=Path(env.get("DATA_DIR") or (BASE_DIR / "data")),
        )

        if errors:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))
        cfg.validate()
        return cfg

    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        problems: list[str] = []
        if not self.telegram_token or ":" not in self.telegram_token:
            problems.append("TELEGRAM_TOKEN looks invalid (expected format '123456:ABC...')")
        if not self.channel_id:
            problems.append("CHANNEL_ID is empty (use '@channel' or a numeric chat id)")
        if not self.gold_symbol:
            problems.append("GOLD_SYMBOL is empty")
        if self.min_signal_score < 10:
            problems.append("MIN_SIGNAL_SCORE is too low")
        if self.ema_fast >= self.ema_mid or self.ema_mid >= self.ema_slow:
            problems.append("EMA periods must satisfy EMA_FAST < EMA_MID < EMA_SLOW")
        if not any(self.strategy_enabled.values()):
            problems.append("All strategies are disabled — nothing will ever be signalled")
        if self.tp_atr_mult <= self.sl_atr_mult and self.tp_atr_mult > 0:
            pass  # TP can be smaller than SL, user's choice; not an error
        if problems:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))

    # ------------------------------------------------------------------ #
    def chat_id(self) -> str:
        """Numeric channel ids may be stored as strings; return as given."""
        return self.channel_id

    def admin_id(self) -> str | None:
        return self.admin_chat_id

    def summary(self) -> str:
        return (
            f"exchange={self.primary_exchange} symbol={self.gold_symbol} "
            f"timeframes={','.join(self.timeframes)} "
            f"poll={self.poll_interval_seconds}s cooldown={self.signal_cooldown_minutes}m "
            f"min_score={self.min_signal_score} confluence={self.min_confluence_strategies} "
            f"strategies={','.join(k for k, v in self.strategy_enabled.items() if v)}"
        )
