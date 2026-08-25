"""Technical indicators — pure NumPy/Pandas, causal (no lookahead).

All functions accept a 1-D sequence of prices (list / tuple / numpy array /
pandas Series) and return a ``numpy.ndarray`` of the same length where
values before the warm-up period are ``NaN``.  Everything is computed in
one pass so strategies can share a single :class:`IndicatorSet` and both the
live engine and the backtester evaluate candles identically.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


def _as_series(values) -> pd.Series:
    if isinstance(values, pd.Series):
        return values.astype(float)
    return pd.Series(np.asarray(values, dtype=float))


def sma(values, period: int) -> np.ndarray:
    s = _as_series(values)
    return s.rolling(period, min_periods=period).mean().to_numpy()


def ema(values, period: int) -> np.ndarray:
    """Classic EMA seeded with the SMA of the first `period` values (TA-Lib convention)."""
    arr = _as_series(values).to_numpy(copy=True)
    n = len(arr)
    out = np.full(n, np.nan)
    if n < period or period < 1:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = float(np.mean(arr[:period]))
    for i in range(period, n):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def rsi(values, period: int = 14) -> np.ndarray:
    """Wilder's RSI (seeded with the SMA of the first `period` gains/losses)."""
    s = _as_series(values)
    delta = s.diff()
    gain = delta.clip(lower=0.0).to_numpy(copy=True)
    loss = (-delta.clip(upper=0.0)).to_numpy(copy=True)
    n = len(gain)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    # Wilder seed: simple average of the first `period` values
    avg_gain = float(np.mean(gain[1 : period + 1]))
    avg_loss = float(np.mean(loss[1 : period + 1]))
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        out[i] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values, fast: int = 12, slow: int = 26, signal: int = 9):
    s = _as_series(values)
    line = s.ewm(span=fast, adjust=False, min_periods=fast).mean() - s.ewm(
        span=slow, adjust=False, min_periods=slow
    ).mean()
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = line - sig
    return line.to_numpy(), sig.to_numpy(), hist.to_numpy()


def atr(high, low, close, period: int = 14) -> np.ndarray:
    h = _as_series(high)
    l = _as_series(low)
    c = _as_series(close)
    prev_close = c.shift(1)
    tr = pd.concat([h - l, (h - prev_close).abs(), (l - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean().to_numpy()


def bollinger(values, period: int = 20, std_mult: float = 2.0):
    s = _as_series(values)
    mid = s.rolling(period, min_periods=period).mean()
    std = s.rolling(period, min_periods=period).std(ddof=0)
    up = mid + std_mult * std
    low = mid - std_mult * std
    return up.to_numpy(), mid.to_numpy(), low.to_numpy()


def stochastic(high, low, close, k_period: int = 14, d_period: int = 3):
    h = _as_series(high).rolling(k_period, min_periods=k_period).max()
    l = _as_series(low).rolling(k_period, min_periods=k_period).min()
    k = 100.0 * (pd.Series(close) - l) / (h - l).replace(0.0, np.nan)
    d = k.rolling(d_period, min_periods=d_period).mean()
    return k.to_numpy(), d.to_numpy()


def vwap(high, low, close, volume, timestamps) -> np.ndarray:
    """Session-anchored VWAP. Anchors at the UTC calendar day (00:00 UTC)."""
    typical = (np.asarray(high) + np.asarray(low) + np.asarray(close)) / 3.0
    vol = np.asarray(volume, dtype=float)
    ts = np.asarray(timestamps, dtype="datetime64[s]")
    dates = ts.astype("datetime64[D]")
    df = pd.DataFrame({"tpv": typical * vol, "vol": vol, "d": dates})
    vwap_series = df.groupby("d")["tpv"].cumsum() / df.groupby("d")["vol"].cumsum().replace(0.0, np.nan)
    return vwap_series.to_numpy()


def donchian(high, low, period: int = 20, shift: int = 1):
    """Donchian channel of the *previous* `shift` candles (excludes current)."""
    h = _as_series(high).rolling(period, min_periods=period).max().shift(shift)
    l = _as_series(low).rolling(period, min_periods=period).min().shift(shift)
    return h.to_numpy(), l.to_numpy()


def swing_highs(high, k: int = 2) -> np.ndarray:
    arr = np.asarray(high, dtype=float)
    n = len(arr)
    out = np.zeros(n, dtype=bool)
    for i in range(k, n - k):
        window = arr[i - k : i + k + 1]
        if arr[i] == window.max() and window[0] < arr[i] and window[-1] < arr[i]:
            out[i] = True
    return out


def swing_lows(low, k: int = 2) -> np.ndarray:
    arr = np.asarray(low, dtype=float)
    n = len(arr)
    out = np.zeros(n, dtype=bool)
    for i in range(k, n - k):
        window = arr[i - k : i + k + 1]
        if arr[i] == window.min() and window[0] > arr[i] and window[-1] > arr[i]:
            out[i] = True
    return out


# --------------------------------------------------------------------------- #
# Candlestick pattern helpers (used by strategies)
# --------------------------------------------------------------------------- #

def candle_body(open_, close) -> float:
    return abs(float(close) - float(open_))


def candle_range(high, low) -> float:
    return float(high) - float(low)


def lower_wick(open_, low, close) -> float:
    return min(float(open_), float(close)) - float(low)


def upper_wick(high, open_, close) -> float:
    return float(high) - max(float(open_), float(close))


def is_bullish(o, c) -> bool:
    return float(c) > float(o)


def is_bearish(o, c) -> bool:
    return float(c) < float(o)


def is_bullish_pin(o, h, l, c) -> bool:
    body = candle_body(o, c)
    rng = candle_range(h, l)
    if rng <= 0 or body <= 0:
        return False
    return lower_wick(o, l, c) > 1.5 * body and lower_wick(o, l, c) > 0.6 * rng


def is_bearish_pin(o, h, l, c) -> bool:
    body = candle_body(o, c)
    rng = candle_range(h, l)
    if rng <= 0 or body <= 0:
        return False
    return upper_wick(h, o, c) > 1.5 * body and upper_wick(h, o, c) > 0.6 * rng


def is_bullish_engulf(o, h, l, c, o_prev, c_prev) -> bool:
    return (
        is_bullish(o, c)
        and is_bearish(o_prev, c_prev)
        and float(c) >= float(o_prev)
        and float(o) <= float(c_prev)
        and candle_range(h, l) > candle_range(max(o, o_prev), min(c, c_prev)) * 0  # any range
    )


def is_bearish_engulf(o, h, l, c, o_prev, c_prev) -> bool:
    return (
        is_bearish(o, c)
        and is_bullish(o_prev, c_prev)
        and float(o) >= float(c_prev)
        and float(c) <= float(o_prev)
    )


# --------------------------------------------------------------------------- #
# Pre-computed indicator bundle
# --------------------------------------------------------------------------- #

@dataclass
class IndicatorSet:
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    timestamps: np.ndarray

    ema_fast: np.ndarray = field(default=None)
    ema_mid: np.ndarray = field(default=None)
    ema_slow: np.ndarray = field(default=None)
    rsi: np.ndarray = field(default=None)
    atr: np.ndarray = field(default=None)
    bb_up: np.ndarray = field(default=None)
    bb_mid: np.ndarray = field(default=None)
    bb_low: np.ndarray = field(default=None)
    macd_line: np.ndarray = field(default=None)
    macd_signal: np.ndarray = field(default=None)
    macd_hist: np.ndarray = field(default=None)
    stoch_k: np.ndarray = field(default=None)
    stoch_d: np.ndarray = field(default=None)
    vwap: np.ndarray = field(default=None)
    don_hi: np.ndarray = field(default=None)
    don_lo: np.ndarray = field(default=None)
    vol_sma: np.ndarray = field(default=None)
    swing_hi: np.ndarray = field(default=None)
    swing_lo: np.ndarray = field(default=None)

    @classmethod
    def compute(cls, candles, params) -> "IndicatorSet":
        """`candles`: list of ccxt OHLCV rows [ts, o, h, l, c, v]."""
        arr = np.asarray(candles, dtype=float)
        ts = arr[:, 0].astype(np.int64)
        o, h, l, c, v = (arr[:, i] for i in range(1, 6))
        ind = cls(open=o, high=h, low=l, close=c, volume=v, timestamps=ts)
        ind.ema_fast = ema(c, params.ema_fast)
        ind.ema_mid = ema(c, params.ema_mid)
        ind.ema_slow = ema(c, params.ema_slow)
        ind.rsi = rsi(c, params.rsi_period)
        ind.atr = atr(h, l, c, params.atr_period)
        ind.bb_up, ind.bb_mid, ind.bb_low = bollinger(c, params.bb_period, params.bb_std)
        ind.macd_line, ind.macd_signal, ind.macd_hist = macd(c, params.macd_fast, params.macd_slow, params.macd_signal)
        ind.stoch_k, ind.stoch_d = stochastic(h, l, c, params.stoch_k, params.stoch_d)
        ind.vwap = vwap(h, l, c, v, ts)
        ind.don_hi, ind.don_lo = donchian(h, l, params.donchian_period)
        ind.vol_sma = sma(v, params.volume_sma_period)
        ind.swing_hi = swing_highs(h)
        ind.swing_lo = swing_lows(l)
        return ind

    def finite_at(self, idx: int, *arrays) -> bool:
        for a in arrays:
            if a is None or idx >= len(a):
                return False
            if not np.isfinite(a[idx]):
                return False
        return True

    def ready(self, idx: int) -> bool:
        """True when every indicator is available at `idx` (warm-up passed)."""
        needed = (
            self.ema_fast, self.ema_mid, self.ema_slow, self.rsi, self.atr,
            self.bb_up, self.bb_mid, self.bb_low, self.macd_line, self.macd_signal,
            self.macd_hist, self.stoch_k, self.stoch_d, self.vwap, self.don_hi,
            self.don_lo, self.vol_sma,
        )
        return self.finite_at(idx, *needed) and idx >= 2
