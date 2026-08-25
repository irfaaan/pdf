"""Offline unit tests for the indicator library (stdlib unittest — no pytest needed).

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

import numpy as np

from bot.indicators import (
    atr,
    bollinger,
    donchian,
    ema,
    macd,
    rsi,
    sma,
    stochastic,
    vwap,
)


class TestSMA(unittest.TestCase):
    def test_basic(self):
        out = sma([1, 2, 3, 4, 5], 3)
        self.assertTrue(np.isnan(out[0]))
        self.assertTrue(np.isnan(out[1]))
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[4], 4.0)


class TestEMA(unittest.TestCase):
    def test_known_series(self):
        # EMA(3) of 1..6: first valid at idx 2
        out = ema([1, 2, 3, 4, 5, 6], 3)
        self.assertTrue(np.isnan(out[1]))
        self.assertAlmostEqual(out[2], 2.0)
        # next = 0.5*4 + 0.5*2 = 3.0 ; then 0.5*5+0.5*3=4.0 ; 0.5*6+0.5*4=5.0
        self.assertAlmostEqual(out[3], 3.0)
        self.assertAlmostEqual(out[4], 4.0)
        self.assertAlmostEqual(out[5], 5.0)

    def test_monotonic(self):
        out = ema(np.arange(1, 101, dtype=float), 10)
        self.assertTrue(np.all(np.diff(out[10:]) > 0))


class TestRSI(unittest.TestCase):
    def test_wilder_reference(self):
        # Classic Wilder example (prices from his book, RSI(14) ~70.5)
        prices = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
        ]
        out = rsi(prices, 14)
        self.assertAlmostEqual(out[-1], 70.53, delta=1.5)

    def test_oversold(self):
        down = list(np.linspace(100, 50, 30))
        out = rsi(down, 14)
        self.assertLess(out[-1], 30)

    def test_overbought(self):
        up = list(np.linspace(50, 100, 30))
        out = rsi(up, 14)
        self.assertGreater(out[-1], 70)


class TestMACD(unittest.TestCase):
    def test_shapes_and_lengths(self):
        prices = np.linspace(100, 120, 200)
        line, sig, hist = macd(prices, 12, 26, 9)
        self.assertEqual(len(line), 200)
        self.assertEqual(len(sig), 200)
        self.assertEqual(len(hist), 200)
        # Trending up => MACD line above signal at the end
        self.assertGreater(line[-1], sig[-1])
        self.assertGreater(hist[-1], 0)


class TestATR(unittest.TestCase):
    def test_basic(self):
        h = [10, 11, 12]
        l = [9, 9.5, 10]
        c = [9.5, 10.5, 11.5]
        out = atr(h, l, c, 3)
        self.assertTrue(np.isfinite(out[-1]))
        self.assertGreater(out[-1], 0)


class TestBollinger(unittest.TestCase):
    def test_band_order(self):
        prices = np.random.default_rng(1).normal(100, 2, 100)
        up, mid, low = bollinger(prices, 20, 2.0)
        self.assertTrue(np.all(up[19:] >= mid[19:] - 1e-9))
        self.assertTrue(np.all(mid[19:] >= low[19:] - 1e-9))


class TestStochastic(unittest.TestCase):
    def test_bounds(self):
        h = np.linspace(100, 110, 60)
        l = np.linspace(99, 109, 60)
        c = np.linspace(99.5, 109.5, 60)
        k, d = stochastic(h, l, c, 14, 3)
        valid = k[20:]
        self.assertTrue(np.all((valid >= -0.01) & (valid <= 101.0)))


class TestVWAP(unittest.TestCase):
    def test_anchor_reset(self):
        import datetime as dt

        base = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1000
        ts = [base + i * 60_000 for i in range(300)]
        h = [100.0] * 300
        l = [99.0] * 300
        c = [99.5] * 300
        v = [10.0] * 300
        out = vwap(h, l, c, v, ts)
        self.assertTrue(np.all(np.isfinite(out)))
        # All equal prices -> VWAP == typical price
        self.assertAlmostEqual(out[-1], 99.5, places=6)


class TestDonchian(unittest.TestCase):
    def test_excludes_current(self):
        h = [1, 2, 3, 4, 5, 6]
        l = [0, 1, 2, 3, 4, 5]
        hi, lo = donchian(h, l, 3, shift=1)
        # at idx 4: window of highs [2,3,4] -> 4 (current 5 excluded)
        self.assertAlmostEqual(hi[4], 4.0)
        self.assertAlmostEqual(lo[4], 1.0)


if __name__ == "__main__":
    unittest.main()
