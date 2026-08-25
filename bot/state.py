"""Runtime state shared between the engine, commands and the notifier.

Holds mutable toggles (changed live via Telegram /set, /enable, /disable),
cooldown timestamps, the emitted-signal dedup set and uptime bookkeeping.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

RUNTIME_PARAMS = {
    # key -> (default, human label)
    "poll_interval_seconds": (15.0, "Poll interval (seconds)"),
    "signal_cooldown_minutes": (10.0, "Signal cooldown (minutes)"),
    "min_signal_score": (65, "Minimum confidence score (10-100)"),
    "min_confluence_strategies": (2, "Minimum agreeing strategies (1-6)"),
    "sl_atr_mult": (1.5, "Stop loss (×ATR)"),
    "tp_atr_mult": (2.5, "Take profit (×ATR)"),
    "vwap_dev_atr": (1.8, "VWAP deviation trigger (×ATR)"),
}


@dataclass
class AppState:
    toggles: dict[str, bool] = field(default_factory=lambda: {
        "gold": True,
        "charts": True,
        "ai": True,
        "broadcast": True,
        "tracking": True,
        "errors": True,
        "news": True,
    })
    runtime: dict[str, float | int] = field(default_factory=lambda: {k: v[0] for k, v in RUNTIME_PARAMS.items()})
    strategy_flags: dict[str, bool] = field(default_factory=dict)
    cooldowns: dict[tuple[str, str], float] = field(default_factory=dict)  # (tf, dir) -> ts
    emitted: set[tuple[str, str, int]] = field(default_factory=set)        # (tf, dir, candle_ts)
    started_at: float = field(default_factory=time.time)
    last_scan_at: float = 0.0
    scan_count: int = 0
    last_signal: dict | None = None
    last_error: str = ""
    engine_alive: bool = False

    def toggle(self, key: str, value: bool) -> None:
        if key in self.toggles:
            self.toggles[key] = value

    def get(self, key: str, default=None):
        if key in self.toggles:
            return self.toggles[key]
        if key in self.runtime:
            return self.runtime[key]
        return default

    def set_param(self, key: str, value: float | int) -> bool:
        if key not in RUNTIME_PARAMS:
            return False
        lo, hi = 0.0, 1e9
        if key == "poll_interval_seconds":
            lo, hi = 3.0, 300.0
        elif key == "signal_cooldown_minutes":
            lo, hi = 0.0, 1440.0
        elif key == "min_signal_score":
            lo, hi = 10, 100
            value = int(value)
        elif key == "min_confluence_strategies":
            lo, hi = 1, 6
            value = int(value)
        elif key in ("sl_atr_mult", "tp_atr_mult", "vwap_dev_atr"):
            lo, hi = 0.1, 20.0
        if not (lo <= value <= hi):
            return False
        self.runtime[key] = value
        return True

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at
