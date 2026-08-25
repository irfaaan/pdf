"""Generate realistic example outputs from the bot's actual code paths.

Run:  .venv/bin/python tests/make_examples.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from bot.config import Config
from bot.engine import Params, SignalEngine
from bot.indicators import IndicatorSet
from bot.state import AppState
from bot.strategies import STRATEGIES, ScanContext, merge_signals

GOLD = 3365.45


def realistic_candles(n: int = 500, seed: int = 99) -> list[list[float]]:
    """Synthetic candles with realistic gold prices (~3365) and regimes."""
    rng = np.random.default_rng(seed)
    price = GOLD - 60.0
    candles: list[list[float]] = []
    ts = int(dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc).timestamp()) * 1000
    drift = 0.0
    regime = 0
    for i in range(n):
        if i % 220 == 0:
            regime = rng.integers(0, 4)
        drift = {0: 0.05, 1: 0.12, 2: -0.10, 3: 0.0}[regime]
        o = price
        c = price + drift + float(rng.normal(0, 1.1))
        h = max(o, c) + abs(float(rng.normal(0, 0.9)))
        l = min(o, c) - abs(float(rng.normal(0, 0.9)))
        v = float(rng.uniform(80, 600))
        candles.append([ts, o, h, l, c, v])
        price = c
        ts += 300_000  # 5m candles
    return candles


def find_signal(cfg, candles):
    state = AppState()
    state.strategy_flags = dict(cfg.strategy_enabled)
    params = Params(cfg, state)
    ind = IndicatorSet.compute(candles, params)
    tf_state: dict[str, dict] = {}
    for i in range(200, len(candles) - 1):
        ctx = ScanContext(tf="5m", ind=ind, idx=i, params=params, state=tf_state.setdefault("5m", {}))
        sigs = [fn(ctx) for fn in STRATEGIES.values()]
        sigs = [s for s in sigs if s is not None]
        merged = merge_signals(
            sigs,
            min_score=int(state.runtime["min_signal_score"]),
            min_confluence=int(state.runtime["min_confluence_strategies"]),
        )
        if merged is not None:
            return merged, ind, i, candles
    return None, ind, -1, candles


def main() -> None:
    cfg = Config.load()
    candles = realistic_candles()
    merged, ind, idx, series = find_signal(cfg, candles)
    print(f"signal at idx {idx}, direction={merged.direction if merged else None}")
    if merged is None or idx < 0:
        print("no signal found — tune seed")
        return

    # Build the exact signal dict the engine produces
    atr = float(ind.atr[idx])
    entry = float(ind.close[idx])
    sl_atr, tp_atr = cfg.sl_atr_mult, cfg.tp_atr_mult
    if merged.direction == "long":
        sl, tp = entry - sl_atr * atr, entry + tp_atr * atr
    else:
        sl, tp = entry + sl_atr * atr, entry - tp_atr * atr
    candle_dt = dt.datetime.fromtimestamp(int(series[idx][0]) / 1000, tz=dt.timezone.utc)

    sig = {
        "version": "1.0.0",
        "direction": merged.direction,
        "timeframe": merged.timeframe or "5m",
        "candle_ts": int(series[idx][0]),
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

    # Render the chart exactly like the engine does
    from bot.charting import render_chart

    path = render_chart(
        series, ind,
        title=f"XAU/USDT 5m — {'LONG' if sig['direction'] == 'long' else 'SHORT'} {sig['score']}/100",
        direction=sig["direction"], entry=sig["entry"], sl=sig["sl"], tp=sig["tp"],
        signal_index=idx,
        out_path=cfg.data_dir / "charts" / "example_signal.png",
    )

    # Print the exact Telegram message (HTML source)
    import types

    from bot.notifier import Notifier

    notifier = Notifier.__new__(Notifier)
    notifier.cfg = cfg
    text = Notifier.build_signal_text(notifier, sig)
    print("===== TELEGRAM MESSAGE (HTML) =====")
    print(text)
    print("===== TELEGRAM MESSAGE (plain) =====")
    import re

    plain = re.sub(r"</?[a-z]+>", "", text)
    print(plain)
    print("===== CHART =====")
    print(path)


if __name__ == "__main__":
    main()
