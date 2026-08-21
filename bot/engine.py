"""Signal engine — polls MEXC, evaluates the scalping strategies on every
closed candle of every timeframe, applies filters (news blackout, weekend,
cooldown, dedup) and emits Telegram notifications when a setup forms.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from pathlib import Path

import numpy as np

from bot.ai_advisor import AIAdvisor
from bot.charting import format_price, render_chart
from bot.exchange_client import ExchangeClient
from bot.indicators import IndicatorSet
from bot.notifier import Notifier
from bot.state import AppState
from bot.strategies import STRATEGIES, MergedSignal, ScanContext, Signal, merge_signals
from bot.tracking import SignalTracker

log = logging.getLogger("goldbot.engine")

UTC = dt.timezone.utc
VERSION = "1.0.0"


class Params:
    """Strategy parameter bundle (config values + live runtime overrides)."""

    def __init__(self, cfg, state: AppState):
        self.cfg = cfg
        self.state = state
        self.ema_fast = cfg.ema_fast
        self.ema_mid = cfg.ema_mid
        self.ema_slow = cfg.ema_slow
        self.rsi_period = cfg.rsi_period
        self.atr_period = cfg.atr_period
        self.bb_period = cfg.bb_period
        self.bb_std = cfg.bb_std
        self.macd_fast = cfg.macd_fast
        self.macd_slow = cfg.macd_slow
        self.macd_signal = cfg.macd_signal
        self.stoch_k = cfg.stoch_k
        self.stoch_d = cfg.stoch_d
        self.donchian_period = cfg.donchian_period
        self.volume_sma_period = cfg.volume_sma_period
        self.vwap_dev_atr = float(state.runtime.get("vwap_dev_atr", cfg.vwap_dev_atr))
        self.sl_atr_mult = float(state.runtime.get("sl_atr_mult", cfg.sl_atr_mult))
        self.tp_atr_mult = float(state.runtime.get("tp_atr_mult", cfg.tp_atr_mult))

    def refresh(self) -> None:
        self.vwap_dev_atr = float(self.state.runtime.get("vwap_dev_atr", self.cfg.vwap_dev_atr))
        self.sl_atr_mult = float(self.state.runtime.get("sl_atr_mult", self.cfg.sl_atr_mult))
        self.tp_atr_mult = float(self.state.runtime.get("tp_atr_mult", self.cfg.tp_atr_mult))


class SignalEngine:
    def __init__(self, cfg, ex: ExchangeClient, notifier: Notifier,
                 tracker: SignalTracker, ai: AIAdvisor, state: AppState):
        self.cfg = cfg
        self.ex = ex
        self.notifier = notifier
        self.tracker = tracker
        self.ai = ai
        self.state = state
        self.params = Params(cfg, state)
        self._tf_state: dict[str, dict] = {}
        self._candle_cache: dict[str, list[list[float]]] = {}
        self._last_error_notify = 0.0

    # ------------------------------------------------------------------ #
    async def scan_once(self) -> None:
        """One full pass: fetch candles for every timeframe and evaluate."""
        if not self.state.toggles.get("gold", True):
            self.state.last_scan_at = time.time()
            return
        if not self.ex.resolved_symbol:
            log.warning("Exchange not connected yet — skipping scan")
            return

        self.params.refresh()
        for tf in self.cfg.timeframes:
            try:
                await self._scan_timeframe(tf)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Scan error on %s: %s", tf, exc)
                await self._maybe_notify_error(f"Scan error on {tf}: {exc}")

        self.state.scan_count += 1
        self.state.last_scan_at = time.time()

    async def _scan_timeframe(self, tf: str) -> None:
        candles = await self.ex.fetch_ohlcv(tf, limit=self.cfg.max_candles)
        if len(candles) < 120:
            log.warning("Only %d candles for %s — skipping", len(candles), tf)
            return
        self._candle_cache[tf] = candles

        ind = IndicatorSet.compute(candles, self.params)
        idx = len(candles) - 2  # last CLOSED candle (last row is forming)
        if idx < 2 or not ind.ready(idx):
            return

        # --- run enabled strategies ---
        signals: list[Signal] = []
        ctx = ScanContext(tf=tf, ind=ind, idx=idx, params=self.params, state=self._tf_state.setdefault(tf, {}))
        for name, fn in STRATEGIES.items():
            if not self.state.strategy_flags.get(name, True):
                continue
            try:
                sig = fn(ctx)
            except Exception as exc:
                log.error("Strategy %s crashed on %s: %s", name, tf, exc)
                continue
            if sig is not None:
                signals.append(sig)

        merged = merge_signals(
            signals,
            min_score=int(self.state.runtime.get("min_signal_score", self.cfg.min_signal_score)),
            min_confluence=int(self.state.runtime.get("min_confluence_strategies", self.cfg.min_confluence_strategies)),
        )
        if merged is None:
            return

        merged.timeframe = tf
        merged.candle_ts = int(candles[idx][0])
        await self._maybe_emit(merged, candles, ind, idx)

    # ------------------------------------------------------------------ #
    async def _maybe_emit(self, merged: MergedSignal, candles, ind: IndicatorSet, idx: int) -> None:
        cfg = self.cfg
        state = self.state
        tf = merged.timeframe
        direction = merged.direction
        candle_ts = merged.candle_ts

        # Dedup: one signal per (timeframe, direction, candle)
        key = (tf, direction, candle_ts)
        if key in state.emitted:
            return
        state.emitted.add(key)
        if len(state.emitted) > 5000:
            state.emitted.clear()

        # Cooldown between signals of the same direction on the same timeframe
        cooldown_s = float(state.runtime.get("signal_cooldown_minutes", cfg.signal_cooldown_minutes)) * 60.0
        last = state.cooldowns.get((tf, direction), 0.0)
        if cooldown_s > 0 and (time.time() - last) < cooldown_s:
            log.info("Signal %s %s suppressed: cooldown active (%.0fs left)",
                     tf, direction, cooldown_s - (time.time() - last))
            return

        # News blackout
        blackout_note = ""
        if state.toggles.get("news", cfg.news_blackout_enabled):
            note = self._in_blackout()
            if note:
                log.info("Signal %s %s suppressed: %s", tf, direction, note)
                return

        # Weekend filter
        if cfg.weekend_filter and dt.datetime.now(UTC).weekday() >= 5:
            log.info("Signal suppressed: weekend filter enabled")
            return

        # --- build the trade plan ---
        atr = float(ind.atr[idx])
        if not np.isfinite(atr) or atr <= 0:
            return
        entry = float(ind.close[idx])
        sl_atr = float(state.runtime.get("sl_atr_mult", cfg.sl_atr_mult))
        tp_atr = float(state.runtime.get("tp_atr_mult", cfg.tp_atr_mult))
        if direction == "long":
            sl = entry - sl_atr * atr
            tp = entry + tp_atr * atr
        else:
            sl = entry + sl_atr * atr
            tp = entry - tp_atr * atr

        candle_dt = dt.datetime.fromtimestamp(candle_ts / 1000, tz=UTC)

        sig = {
            "version": VERSION,
            "direction": direction,
            "timeframe": tf,
            "candle_ts": candle_ts,
            "time_str": candle_dt.strftime("%Y-%m-%d %H:%M"),
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "sl_atr_mult": sl_atr,
            "tp_atr_mult": tp_atr,
            "rr": round(tp_atr / sl_atr, 1),
            "atr": round(atr, 2),
            "score": merged.score,
            "strategies": [s.name for s in merged.signals],
            "strategy_names": merged.strategy_names,
            "reasons": merged.reasons,
            "status": "open",
        }
        state.last_signal = sig
        state.cooldowns[(tf, direction)] = time.time()

        # --- notify ---
        chart_path: Path | None = None
        if state.toggles.get("charts", cfg.charts_enabled):
            try:
                chart_path = render_chart(
                    candles, ind,
                    title=f"XAU/USDT {tf} — {'LONG' if direction == 'long' else 'SHORT'} {merged.score}/100",
                    direction=direction,
                    entry=entry, sl=sl, tp=tp,
                    signal_index=idx,
                    out_path=cfg.data_dir / "charts" / f"{candle_dt:%Y%m%d_%H%M%S}_{tf}_{direction}.png",
                )
            except Exception as exc:
                log.error("Chart render failed: %s", exc)
                chart_path = None

        if state.toggles.get("broadcast", cfg.broadcast_enabled):
            try:
                msg = await self.notifier.send_signal(sig, chart_path)
                if msg is not None and state.toggles.get("ai", cfg.ai_enabled):
                    asyncio.create_task(self._ai_vet_and_edit(msg, sig))
            except Exception as exc:
                log.error("Signal notification failed: %s", exc)
                await self._maybe_notify_error(f"Signal notification failed: {exc}")

        # --- track ---
        if state.toggles.get("tracking", cfg.tracking_enabled):
            self.tracker.record(sig)

        log.info("SIGNAL %s %s %s score=%d strategies=%s entry=%s sl=%s tp=%s",
                 tf, direction.upper(), candle_dt, merged.score,
                 [s.name for s in merged.signals], entry, sl, tp)

    async def _ai_vet_and_edit(self, msg, sig: dict) -> None:
        try:
            verdict = await self.ai.vet(
                f"{sig['direction'].upper()} XAU/USDT {sig['timeframe']}, score {sig['score']}/100, "
                f"strategies: {sig['strategy_names']}. Entry {sig['entry']}, SL {sig['sl']}, TP {sig['tp']}, ATR {sig['atr']}.",
                sig["entry"],
                sig["atr"],
            )
            if verdict:
                new_text = self.notifier.build_signal_text(sig) + f"\n🤖 AI vetting: {verdict}"
                await self.notifier.edit_text(msg, new_text)
        except Exception as exc:
            log.debug("AI edit failed: %s", exc)

    # ------------------------------------------------------------------ #
    def _in_blackout(self) -> str:
        """Returns a note when now is inside a configured news window (UTC)."""
        now = dt.datetime.now(UTC)
        for start, end in self.cfg.news_blackout_windows:
            s = dt.datetime.strptime(start, "%H:%M").replace(tzinfo=UTC)
            e = dt.datetime.strptime(end, "%H:%M").replace(tzinfo=UTC)
            t = now.replace(year=s.year, month=s.month, day=s.day)
            if s <= t <= e:
                return f"News blackout {start}–{end} UTC (high-impact data)"
        return ""

    async def _maybe_notify_error(self, text: str) -> None:
        now = time.time()
        if now - self._last_error_notify < 300:  # max 1 alert / 5 min
            return
        self._last_error_notify = now
        self.state.last_error = text
        await self.notifier.notify_error(text)

    # ------------------------------------------------------------------ #
    # Helpers for Telegram commands
    # ------------------------------------------------------------------ #
    def latest_candles(self, tf: str) -> list[list[float]] | None:
        return self._candle_cache.get(tf)

    async def current_price(self) -> float | None:
        try:
            ticker = await self.ex.fetch_ticker()
            return float(ticker.get("last") or ticker.get("close"))
        except Exception as exc:
            log.warning("fetch_ticker failed: %s", exc)
            return None

    def blackout_status(self) -> str:
        note = self._in_blackout()
        if note:
            return f"⏸ Active: {note}"
        return "None (signals allowed)"
