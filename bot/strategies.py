"""Scalping strategies for XAU/USDT.

Every strategy is a pure function of a :class:`ScanContext` and returns a
:class:`Signal` or ``None``.  All evaluation happens on **closed** candles
only (no repainting).  The exact same code path is used by the live engine
and the backtester, so backtest results transfer to live behaviour.

Strategy scoring:
    strong    -> contributes up to 60 points
    moderate  -> contributes up to 35 points
    weak      -> contributes up to 15 points
A confluence bonus is added when several strategies agree on one direction.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from bot.indicators import (
    IndicatorSet,
    is_bearish,
    is_bearish_engulf,
    is_bearish_pin,
    is_bullish,
    is_bullish_engulf,
    is_bullish_pin,
)

STRATEGY_NAMES = {
    "ema_pullback": "EMA Pullback",
    "vwap_reversion": "VWAP Reversion",
    "break_retest": "Break & Retest",
    "order_block": "Order Block",
    "macd_momentum": "MACD Momentum",
    "rsi_divergence": "RSI Divergence",
}

WEIGHT = {"strong": 60, "moderate": 35, "weak": 15}


@dataclass
class Signal:
    name: str                     # strategy key
    direction: str                # 'long' | 'short'
    score: int                    # 0..100 raw strategy score
    strength: str                 # 'strong' | 'moderate' | 'weak'
    reasons: list[str] = field(default_factory=list)

    @property
    def weight(self) -> int:
        return WEIGHT.get(self.strength, 15)


@dataclass
class ScanContext:
    tf: str                       # timeframe key, e.g. '5m'
    ind: IndicatorSet             # precomputed indicators for the whole series
    idx: int                      # index of the closed candle to evaluate
    params: object                # strategy parameter bundle (see engine.Params)
    state: dict = field(default_factory=dict)  # persistent per-timeframe state


# --------------------------------------------------------------------------- #
# 1. EMA Pullback — trend-continuation scalp on the 21 EMA
# --------------------------------------------------------------------------- #

def ema_pullback(ctx: ScanContext) -> Signal | None:
    ind, i = ctx.ind, ctx.idx
    p = ctx.params
    if not ind.finite_at(i, ind.ema_fast, ind.ema_mid, ind.ema_slow, ind.rsi, ind.vol_sma) or i < 10:
        return None

    reasons: list[str] = []

    # --- Long ---
    trend_up = ind.close[i] > ind.ema_slow[i] and ind.ema_slow[i] > ind.ema_slow[i - 3] and ind.ema_mid[i] > ind.ema_mid[i - 6]
    pullback_long = ind.low[i] <= ind.ema_mid[i] and ind.close[i] > ind.ema_mid[i]
    cross_up = ind.ema_fast[i - 1] <= ind.ema_mid[i - 1] and ind.ema_fast[i] > ind.ema_mid[i]
    pin_long = is_bullish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i])
    engulf_long = is_bullish_engulf(ind.open[i], ind.high[i], ind.low[i], ind.close[i],
                                    ind.open[i - 1], ind.close[i - 1])
    rsi_ok_long = 45 < ind.rsi[i] < 80 and ind.rsi[i] > ind.rsi[i - 1]
    vol_ok = ind.volume[i] > ind.vol_sma[i]

    if trend_up and pullback_long and (cross_up or pin_long or engulf_long) and rsi_ok_long:
        reasons.append(f"Uptrend above EMA{p.ema_slow}, pullback reclaimed EMA{p.ema_mid}")
        score = 50
        if cross_up:
            reasons.append(f"EMA{p.ema_fast} crossed above EMA{p.ema_mid}")
            score += 10
        if pin_long or engulf_long:
            reasons.append("Bullish rejection candle at the EMA")
            score += 10
        if vol_ok:
            reasons.append("Volume above average")
            score += 5
        if ind.rsi[i] > 60:
            reasons.append(f"RSI {ind.rsi[i]:.0f} holding above 60")
            score += 5
        strength = "strong" if (cross_up and vol_ok) else "moderate"
        return Signal("ema_pullback", "long", min(score, 60), strength, reasons)

    # --- Short ---
    trend_down = ind.close[i] < ind.ema_slow[i] and ind.ema_slow[i] < ind.ema_slow[i - 3] and ind.ema_mid[i] < ind.ema_mid[i - 6]
    pullback_short = ind.high[i] >= ind.ema_mid[i] and ind.close[i] < ind.ema_mid[i]
    cross_down = ind.ema_fast[i - 1] >= ind.ema_mid[i - 1] and ind.ema_fast[i] < ind.ema_mid[i]
    pin_short = is_bearish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i])
    engulf_short = is_bearish_engulf(ind.open[i], ind.high[i], ind.low[i], ind.close[i],
                                     ind.open[i - 1], ind.close[i - 1])
    rsi_ok_short = 20 < ind.rsi[i] < 55 and ind.rsi[i] < ind.rsi[i - 1]

    if trend_down and pullback_short and (cross_down or pin_short or engulf_short) and rsi_ok_short:
        reasons.append(f"Downtrend below EMA{p.ema_slow}, pullback rejected at EMA{p.ema_mid}")
        score = 50
        if cross_down:
            reasons.append(f"EMA{p.ema_fast} crossed below EMA{p.ema_mid}")
            score += 10
        if pin_short or engulf_short:
            reasons.append("Bearish rejection candle at the EMA")
            score += 10
        if vol_ok:
            reasons.append("Volume above average")
            score += 5
        if ind.rsi[i] < 40:
            reasons.append(f"RSI {ind.rsi[i]:.0f} under 40")
            score += 5
        strength = "strong" if (cross_down and vol_ok) else "moderate"
        return Signal("ema_pullback", "short", min(score, 60), strength, reasons)

    return None


# --------------------------------------------------------------------------- #
# 2. VWAP Reversion — fade an over-extension back to the session VWAP
# --------------------------------------------------------------------------- #

def vwap_reversion(ctx: ScanContext) -> Signal | None:
    ind, i = ctx.ind, ctx.idx
    p = ctx.params
    if not ind.finite_at(i, ind.vwap, ind.atr, ind.rsi, ind.stoch_k) or i < 3:
        return None
    if ind.atr[i] <= 0:
        return None

    dev = (ind.close[i] - ind.vwap[i]) / ind.atr[i]
    reasons: list[str] = []

    # --- Short: price stretched ABOVE VWAP ---
    if dev > p.vwap_dev_atr and ind.rsi[i] > 68:
        bearish_reversal = (
            is_bearish_engulf(ind.open[i], ind.high[i], ind.low[i], ind.close[i],
                              ind.open[i - 1], ind.close[i - 1])
            or is_bearish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i])
            or (is_bearish(ind.open[i], ind.close[i]) and upper_wick_ratio(ind, i) > 1.2)
        )
        if bearish_reversal:
            reasons.append(f"Price {dev:.1f}×ATR above VWAP — over-extended")
            reasons.append(f"RSI {ind.rsi[i]:.0f} overbought with bearish rejection")
            score = 45
            if is_bearish_engulf(ind.open[i], ind.high[i], ind.low[i], ind.close[i],
                                 ind.open[i - 1], ind.close[i - 1]):
                reasons.append("Bearish engulfing candle")
                score += 10
            if ind.stoch_k[i] > 80:
                reasons.append("Stochastic overbought")
                score += 10
            if dev > p.vwap_dev_atr + 0.6:
                score += 5
            strength = "strong" if score >= 60 else "moderate"
            return Signal("vwap_reversion", "short", min(score, 60), strength, reasons)

    # --- Long: price stretched BELOW VWAP ---
    if dev < -p.vwap_dev_atr and ind.rsi[i] < 32:
        bullish_reversal = (
            is_bullish_engulf(ind.open[i], ind.high[i], ind.low[i], ind.close[i],
                              ind.open[i - 1], ind.close[i - 1])
            or is_bullish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i])
            or (is_bullish(ind.open[i], ind.close[i]) and lower_wick_ratio(ind, i) > 1.2)
        )
        if bullish_reversal:
            reasons.append(f"Price {-dev:.1f}×ATR below VWAP — oversold extension")
            reasons.append(f"RSI {ind.rsi[i]:.0f} oversold with bullish rejection")
            score = 45
            if is_bullish_engulf(ind.open[i], ind.high[i], ind.low[i], ind.close[i],
                                 ind.open[i - 1], ind.close[i - 1]):
                reasons.append("Bullish engulfing candle")
                score += 10
            if ind.stoch_k[i] < 20:
                reasons.append("Stochastic oversold")
                score += 10
            if dev < -p.vwap_dev_atr - 0.6:
                score += 5
            strength = "strong" if score >= 60 else "moderate"
            return Signal("vwap_reversion", "long", min(score, 60), strength, reasons)

    return None


def upper_wick_ratio(ind: IndicatorSet, i: int) -> float:
    rng = ind.high[i] - ind.low[i]
    body = abs(ind.close[i] - ind.open[i])
    if rng <= 0:
        return 0.0
    return (ind.high[i] - max(ind.open[i], ind.close[i])) / max(body, 1e-9)


def lower_wick_ratio(ind: IndicatorSet, i: int) -> float:
    body = abs(ind.close[i] - ind.open[i])
    if body <= 0:
        return 0.0
    return (min(ind.open[i], ind.close[i]) - ind.low[i]) / body


# --------------------------------------------------------------------------- #
# 3. Break & Retest — Donchian breakout with volume, then level retest
# --------------------------------------------------------------------------- #

def break_retest(ctx: ScanContext) -> Signal | None:
    ind, i = ctx.ind, ctx.idx
    p = ctx.params
    if not ind.finite_at(i, ind.don_hi, ind.don_lo, ind.atr, ind.vol_sma):
        return None

    state = ctx.state.setdefault("break_retest", {"pending": []})
    pending: list[dict] = state["pending"]
    reasons: list[str] = []

    # Record fresh breakouts (excluding the current candle's own retest)
    if i >= 1:
        if ind.finite_at(i - 1, ind.don_hi) and ind.close[i - 1] > ind.don_hi[i - 1] \
                and (ind.close[i - 1] - ind.don_hi[i - 1]) > 0.2 * ind.atr[i - 1] \
                and ind.volume[i - 1] > 1.5 * ind.vol_sma[i - 1]:
            pending.append({"level": float(ind.don_hi[i - 1]), "dir": "long", "at": i - 1})
            reasons.append(f"Breakout above {p.donchian_period}-bar high with volume")
        if ind.finite_at(i - 1, ind.don_lo) and ind.close[i - 1] < ind.don_lo[i - 1] \
                and (ind.don_lo[i - 1] - ind.close[i - 1]) > 0.2 * ind.atr[i - 1] \
                and ind.volume[i - 1] > 1.5 * ind.vol_sma[i - 1]:
            pending.append({"level": float(ind.don_lo[i - 1]), "dir": "short", "at": i - 1})

    # Drop stale pending levels
    state["pending"] = [x for x in pending if i - x["at"] <= 12]
    pending = state["pending"]

    # --- Long retest ---
    for entry in pending:
        if entry["dir"] != "long" or i - entry["at"] < 1:
            continue
        level = entry["level"]
        if ind.low[i] <= level <= ind.high[i] and ind.close[i] > level:
            if is_bullish(ind.open[i], ind.close[i]) or is_bullish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i]):
                reasons.append(f"Retest of broken resistance {level:.2f} held")
                reasons.append("Bullish close back above the level")
                score = 55
                if ind.volume[i] > ind.vol_sma[i]:
                    reasons.append("Retest on above-average volume")
                    score += 5
                return Signal("break_retest", "long", min(score, 60),
                              "strong" if score >= 60 else "moderate", reasons)

    # --- Short retest ---
    for entry in pending:
        if entry["dir"] != "short" or i - entry["at"] < 1:
            continue
        level = entry["level"]
        if ind.low[i] <= level <= ind.high[i] and ind.close[i] < level:
            if is_bearish(ind.open[i], ind.close[i]) or is_bearish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i]):
                reasons.append(f"Retest of broken support {level:.2f} rejected")
                reasons.append("Bearish close back below the level")
                score = 55
                if ind.volume[i] > ind.vol_sma[i]:
                    reasons.append("Retest on above-average volume")
                    score += 5
                return Signal("break_retest", "short", min(score, 60),
                              "strong" if score >= 60 else "moderate", reasons)

    return None


# --------------------------------------------------------------------------- #
# 4. Order Block — supply/demand zone retest
# --------------------------------------------------------------------------- #

def order_block(ctx: ScanContext) -> Signal | None:
    ind, i = ctx.ind, ctx.idx
    if not ind.finite_at(i, ind.atr, ind.open, ind.close) or i < 30:
        return None

    reasons: list[str] = []
    lookback_start = max(10, i - 50)

    # --- Bullish order block: last bearish candle before a strong up-move ---
    for j in range(i - 4, lookback_start - 1, -1):
        if not ind.finite_at(j, ind.atr):
            continue
        rng = ind.high[j] - ind.low[j]
        body = abs(ind.close[j] - ind.open[j])
        if not (is_bearish(ind.open[j], ind.close[j]) and rng > 1.2 * ind.atr[j] and body > 0.3 * rng):
            continue
        up_move = float(np.max(ind.high[j + 1 : min(j + 6, i + 1)])) - float(ind.close[j])
        if up_move < 1.5 * ind.atr[j]:
            continue
        zone_lo, zone_hi = float(ind.low[j]), float(ind.open[j])
        if zone_hi <= zone_lo:
            zone_hi = float(ind.low[j]) + body
        # Retest: price dips into the zone and closes back above it
        if ind.low[i] <= zone_hi and ind.close[i] > zone_hi and i - j <= 25:
            if is_bullish(ind.open[i], ind.close[i]) or is_bullish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i]):
                reasons.append(f"Bullish order block {zone_lo:.2f}–{zone_hi:.2f} defended")
                reasons.append(f"Strong move off the zone ({up_move / ind.atr[j]:.1f}×ATR)")
                score = 55
                if i - j <= 10:
                    reasons.append("Fresh zone")
                    score += 5
                return Signal("order_block", "long", min(score, 60),
                              "strong" if score >= 60 else "moderate", reasons)

    # --- Bearish order block: last bullish candle before a strong down-move ---
    for j in range(i - 4, lookback_start - 1, -1):
        if not ind.finite_at(j, ind.atr):
            continue
        rng = ind.high[j] - ind.low[j]
        body = abs(ind.close[j] - ind.open[j])
        if not (is_bullish(ind.open[j], ind.close[j]) and rng > 1.2 * ind.atr[j] and body > 0.3 * rng):
            continue
        down_move = float(ind.close[j]) - float(np.min(ind.low[j + 1 : min(j + 6, i + 1)]))
        if down_move < 1.5 * ind.atr[j]:
            continue
        zone_lo, zone_hi = float(ind.open[j]), float(ind.high[j])
        if zone_hi <= zone_lo:
            zone_lo = float(ind.high[j]) - body
        if ind.high[i] >= zone_lo and ind.close[i] < zone_lo and i - j <= 25:
            if is_bearish(ind.open[i], ind.close[i]) or is_bearish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i]):
                reasons.append(f"Bearish order block {zone_lo:.2f}–{zone_hi:.2f} defended")
                reasons.append(f"Strong move off the zone ({down_move / ind.atr[j]:.1f}×ATR)")
                score = 55
                if i - j <= 10:
                    reasons.append("Fresh zone")
                    score += 5
                return Signal("order_block", "short", min(score, 60),
                              "strong" if score >= 60 else "moderate", reasons)

    return None


# --------------------------------------------------------------------------- #
# 5. MACD Momentum — fresh MACD cross with building histogram
# --------------------------------------------------------------------------- #

def macd_momentum(ctx: ScanContext) -> Signal | None:
    ind, i = ctx.ind, ctx.idx
    p = ctx.params
    if not ind.finite_at(i, ind.macd_line, ind.macd_signal, ind.macd_hist, ind.ema_mid, ind.vol_sma) or i < 4:
        return None

    reasons: list[str] = []
    cross_up = ind.macd_line[i - 1] <= ind.macd_signal[i - 1] and ind.macd_line[i] > ind.macd_signal[i]
    cross_down = ind.macd_line[i - 1] >= ind.macd_signal[i - 1] and ind.macd_line[i] < ind.macd_signal[i]
    hist_building_up = ind.macd_hist[i] > ind.macd_hist[i - 1] > ind.macd_hist[i - 2]
    hist_building_down = ind.macd_hist[i] < ind.macd_hist[i - 1] < ind.macd_hist[i - 2]

    if cross_up and hist_building_up and ind.close[i] > ind.ema_mid[i]:
        reasons.append("MACD crossed above signal with rising histogram")
        score = 50
        if ind.macd_line[i] > 0:
            reasons.append("MACD above the zero line")
            score += 5
        if ind.volume[i] > ind.vol_sma[i]:
            reasons.append("Volume confirms the move")
            score += 5
        return Signal("macd_momentum", "long", min(score, 60),
                      "strong" if score >= 60 else "moderate", reasons)

    if cross_down and hist_building_down and ind.close[i] < ind.ema_mid[i]:
        reasons.append("MACD crossed below signal with falling histogram")
        score = 50
        if ind.macd_line[i] < 0:
            reasons.append("MACD below the zero line")
            score += 5
        if ind.volume[i] > ind.vol_sma[i]:
            reasons.append("Volume confirms the move")
            score += 5
        return Signal("macd_momentum", "short", min(score, 60),
                      "strong" if score >= 60 else "moderate", reasons)

    return None


# --------------------------------------------------------------------------- #
# 6. RSI Divergence — hidden / regular divergence at extremes
# --------------------------------------------------------------------------- #

def rsi_divergence(ctx: ScanContext) -> Signal | None:
    ind, i = ctx.ind, ctx.idx
    if not ind.finite_at(i, ind.rsi, ind.low, ind.high) or i < 20:
        return None

    reasons: list[str] = []
    lo = max(10, i - 60)

    # --- Bullish divergence: lower price low, higher RSI low ---
    lows = np.flatnonzero(ind.swing_lo[lo : i + 1]) + lo
    if len(lows) >= 2:
        i1, i2 = int(lows[-2]), int(lows[-1])
        if ind.low[i2] < ind.low[i1] and ind.rsi[i2] > ind.rsi[i1] + 3 and ind.rsi[i1] < 38 and i - i2 <= 8:
            if is_bullish(ind.open[i], ind.close[i]) or is_bullish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i]):
                reasons.append(f"Bullish RSI divergence (price LL, RSI HL)")
                reasons.append(f"RSI recovered from {ind.rsi[i1]:.0f} to {ind.rsi[i2]:.0f}")
                score = 58
                if ind.rsi[i] > 40:
                    reasons.append("RSI turning up")
                    score += 2
                return Signal("rsi_divergence", "long", min(score, 60), "strong", reasons)

    # --- Bearish divergence: higher price high, lower RSI high ---
    highs = np.flatnonzero(ind.swing_hi[lo : i + 1]) + lo
    if len(highs) >= 2:
        i1, i2 = int(highs[-2]), int(highs[-1])
        if ind.high[i2] > ind.high[i1] and ind.rsi[i2] < ind.rsi[i1] - 3 and ind.rsi[i1] > 62 and i - i2 <= 8:
            if is_bearish(ind.open[i], ind.close[i]) or is_bearish_pin(ind.open[i], ind.high[i], ind.low[i], ind.close[i]):
                reasons.append("Bearish RSI divergence (price HH, RSI LH)")
                reasons.append(f"RSI fell from {ind.rsi[i1]:.0f} to {ind.rsi[i2]:.0f}")
                score = 58
                if ind.rsi[i] < 60:
                    reasons.append("RSI turning down")
                    score += 2
                return Signal("rsi_divergence", "short", min(score, 60), "strong", reasons)

    return None


# --------------------------------------------------------------------------- #
# Registry + confluence merging
# --------------------------------------------------------------------------- #

STRATEGIES: dict[str, object] = {
    "ema_pullback": ema_pullback,
    "vwap_reversion": vwap_reversion,
    "break_retest": break_retest,
    "order_block": order_block,
    "macd_momentum": macd_momentum,
    "rsi_divergence": rsi_divergence,
}


@dataclass
class MergedSignal:
    direction: str
    score: int
    timeframe: str
    candle_ts: int
    signals: list[Signal]
    reasons: list[str]

    @property
    def strategy_names(self) -> str:
        parts = [f"{STRATEGY_NAMES[s.name]} ({s.strength})" for s in self.signals]
        return ", ".join(parts)


def merge_signals(
    signals: list[Signal],
    *,
    min_score: int = 65,
    min_confluence: int = 2,
) -> MergedSignal | None:
    """Combine same-direction strategy signals into one merged setup."""
    if not signals:
        return None

    votes: dict[str, list[Signal]] = {"long": [], "short": []}
    for sig in signals:
        votes[sig.direction].append(sig)

    best: MergedSignal | None = None
    for direction, group in votes.items():
        if not group:
            continue
        if len(group) < min_confluence:
            continue
        total = sum(s.weight for s in group)
        bonus = 10 if len(group) >= 2 else 0
        bonus += 5 if len(group) >= 3 else 0
        score = min(100, total + bonus)
        if score < min_score:
            continue
        reasons: list[str] = []
        for s in group:
            reasons.extend(s.reasons)
        merged = MergedSignal(
            direction=direction,
            score=score,
            timeframe="",
            candle_ts=0,
            signals=group,
            reasons=reasons,
        )
        if best is None or merged.score > best.score:
            best = merged

    return best
