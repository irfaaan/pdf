"""Offline tests for the strategy stack (synthetic candles, no network)."""
from __future__ import annotations

import unittest

import numpy as np

from bot.config import Config
from bot.engine import Params
from bot.indicators import IndicatorSet
from bot.state import AppState
from bot.strategies import STRATEGIES, ScanContext, merge_signals


def make_candles(n: int = 700, seed: int = 7) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    price = 2400.0
    candles: list[list[float]] = []
    ts = 1_700_000_000_000
    for i in range(n):
        if i % 200 < 100:
            drift = 0.3
        elif i % 200 < 150:
            drift = -0.4
        else:
            drift = 0.0
        o = price
        c = price + drift + float(rng.normal(0, 1.2))
        h = max(o, c) + abs(float(rng.normal(0, 1.0)))
        l = min(o, c) - abs(float(rng.normal(0, 1.0)))
        v = float(rng.uniform(100, 500))
        candles.append([ts, o, h, l, c, v])
        price = c
        ts += 60_000
    return candles


class TestConfig(unittest.TestCase):
    def test_smoke_config_loads(self):
        # The real .env must parse cleanly (this is the user's setup)
        cfg = Config.load()
        self.assertTrue(cfg.telegram_token)
        self.assertTrue(cfg.channel_id)
        self.assertEqual(cfg.primary_exchange, "mexc")
        self.assertIn("XAU/USDT", cfg.gold_symbol)
        for tf in cfg.timeframes:
            self.assertIn(tf, ("1m", "3m", "5m", "15m", "30m", "1h"))
        self.assertGreaterEqual(cfg.min_signal_score, 10)
        self.assertGreater(cfg.tp_atr_mult, 0)
        # the markdown-wrapped OpenRouter URL must be normalized
        self.assertTrue(cfg.openrouter_base_url.startswith("https://"))
        self.assertNotIn("[", cfg.openrouter_base_url)
        self.assertNotIn("]", cfg.openrouter_base_url)


class TestStrategyStack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = Config.load()
        cls.candles = make_candles()
        cls.state = AppState()
        cls.params = Params(cls.cfg, cls.state)
        cls.ind = IndicatorSet.compute(cls.candles, cls.params)

    def test_indicators_ready(self):
        self.assertTrue(self.ind.ready(300))
        self.assertEqual(len(self.ind.close), len(self.candles))

    def test_all_strategies_run_clean(self):
        """Every strategy must execute without exceptions across all candles."""
        tf_state: dict[str, dict] = {}
        runs = 0
        for i in range(200, len(self.candles) - 1):
            ctx = ScanContext(tf="5m", ind=self.ind, idx=i, params=self.params,
                              state=tf_state.setdefault("5m", {}))
            for name, fn in STRATEGIES.items():
                try:
                    sig = fn(ctx)
                except Exception as exc:  # pragma: no cover
                    self.fail(f"strategy {name} crashed at {i}: {exc}")
                runs += 1
                if sig is not None:
                    self.assertIn(sig.direction, ("long", "short"))
                    self.assertGreaterEqual(sig.score, 0)
                    self.assertLessEqual(sig.score, 100)
                    self.assertIn(sig.strength, ("strong", "moderate", "weak"))
        self.assertGreater(runs, 1000)

    def test_merge_signals(self):
        from bot.strategies import Signal

        sigs = [
            Signal("ema_pullback", "long", 60, "strong", ["a"]),
            Signal("macd_momentum", "long", 50, "moderate", ["b"]),
            Signal("vwap_reversion", "short", 45, "moderate", ["c"]),
        ]
        merged = merge_signals(sigs, min_score=60, min_confluence=2)
        self.assertIsNotNone(merged)
        self.assertEqual(merged.direction, "long")
        self.assertGreaterEqual(merged.score, 60)
        self.assertEqual(len(merged.signals), 2)

        # Not enough confluence
        self.assertIsNone(merge_signals([sigs[0]], min_score=60, min_confluence=2))
        # Score too low
        self.assertIsNone(merge_signals(sigs, min_score=200, min_confluence=2))

    def test_short_candle_series_safe(self):
        """Very short series must not crash anything."""
        short = self.candles[:80]
        ind = IndicatorSet.compute(short, self.params)
        tf_state: dict[str, dict] = {}
        for i in range(2, len(short)):
            ctx = ScanContext(tf="5m", ind=ind, idx=i, params=self.params,
                              state=tf_state.setdefault("5m", {}))
            for fn in STRATEGIES.values():
                fn(ctx)  # must not raise


if __name__ == "__main__":
    unittest.main()
