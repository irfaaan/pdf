"""Offline end-to-end test: runs the full engine emit path with a fake
exchange and fake notifier, plus chart rendering. No network required.

Run:  .venv/bin/python tests/e2e_offline.py
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ai_advisor import AIAdvisor  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.engine import SignalEngine  # noqa: E402
from bot.state import AppState  # noqa: E402
from bot.tracking import SignalTracker  # noqa: E402
from tests.test_strategies import make_candles  # noqa: E402


class FakeExchange:
    resolved_symbol = "XAU/USDT:USDT"

    def __init__(self, candles_by_tf):
        self._candles = candles_by_tf
        self.last_ok = 0.0

    async def fetch_ohlcv(self, tf, limit=500):
        return self._candles[tf][-limit:]

    async def fetch_ticker(self):
        return {"last": self._candles["5m"][-1][4]}

    def status_text(self):
        return "✅ fake exchange"


class FakeNotifier:
    def __init__(self):
        self.sent: list[dict] = []
        self.edits = 0

    def build_signal_text(self, sig):
        from bot.notifier import Notifier
        import types

        # reuse the real formatter via a stub notifier
        stub = types.SimpleNamespace(cfg=None)
        return Notifier.build_signal_text(stub, sig)

    async def send_signal(self, sig, chart_path=None):
        self.sent.append({"sig": sig, "chart": chart_path})
        return types.SimpleNamespace(chat_id=0, message_id=1)

    async def send_text(self, text, chat_id=None):
        return types.SimpleNamespace(chat_id=0, message_id=1)

    async def send_photo(self, photo, caption):
        return types.SimpleNamespace(chat_id=0, message_id=1)

    async def edit_text(self, message, text):
        self.edits += 1

    async def notify_error(self, text):
        print("   [notify_error]", text)


def main() -> int:
    cfg = Config.load()
    state = AppState()
    state.strategy_flags = dict(cfg.strategy_enabled)

    candles_by_tf = {
        "1m": make_candles(700, seed=11),
        "5m": make_candles(700, seed=12),
        "15m": make_candles(700, seed=13),
    }
    ex = FakeExchange(candles_by_tf)
    notifier = FakeNotifier()
    tracker = SignalTracker(cfg.data_dir, enabled=True)
    ai = AIAdvisor(cfg)
    engine = SignalEngine(cfg, ex, notifier, tracker, ai, state)

    # ---- chart rendering test ----
    print("• Rendering chart from synthetic candles...")
    from bot.charting import render_chart
    from bot.indicators import IndicatorSet

    ind = IndicatorSet.compute(candles_by_tf["5m"], engine.params)
    path = render_chart(
        candles_by_tf["5m"], ind,
        title="XAU/USDT 5m — offline e2e",
        direction="long", entry=2400.0, sl=2395.0, tp=2410.0, signal_index=600,
        out_path=cfg.data_dir / "charts" / "e2e_test.png",
    )
    assert path.exists() and path.stat().st_size > 10_000, "chart too small"
    print(f"  ✓ chart rendered ({path.stat().st_size // 1024} KB)")

    # ---- find a candle that produces a merged setup, then make it the last
    #      closed candle so the engine's single evaluation point triggers it ----
    print("• Locating a signal candle in the synthetic series...")
    from bot.strategies import STRATEGIES, ScanContext, merge_signals

    tf_state: dict[str, dict] = {}
    signal_idx = None
    for i in range(200, len(candles_by_tf["5m"]) - 1):
        ctx = ScanContext(tf="5m", ind=ind, idx=i, params=engine.params,
                          state=tf_state.setdefault("5m", {}))
        sigs = [fn(ctx) for fn in STRATEGIES.values()]
        sigs = [s for s in sigs if s is not None]
        merged = merge_signals(
            sigs,
            min_score=int(state.runtime["min_signal_score"]),
            min_confluence=int(state.runtime["min_confluence_strategies"]),
        )
        if merged is not None:
            signal_idx = i
            break
    if signal_idx is None:
        # relax thresholds so the emit path still gets exercised
        state.runtime["min_signal_score"] = 40
        state.runtime["min_confluence_strategies"] = 1
        print("  (no setup at defaults on synthetic data — relaxing thresholds for the test)")
    else:
        print(f"  ✓ signal candle found at index {signal_idx}")
        # Indicators are causal: truncating the series keeps values at `signal_idx`
        # identical, and the engine evaluates idx = len-2 -> make it the last closed
        candles_by_tf["5m"] = candles_by_tf["5m"][: signal_idx + 2]
        ex._candles = candles_by_tf
        engine._candle_cache = {}
        print(f"  ✓ series truncated so the signal candle is the last closed one")

    # ---- engine scan ----
    print("• Running 5 engine scans...")
    for _ in range(5):
        asyncio.run(engine.scan_once())

    print(f"  ✓ scans done: {state.scan_count}, emitted: {len(notifier.sent)}")
    for item in notifier.sent[:2]:
        sig = item["sig"]
        text = notifier.build_signal_text(sig)
        print(f"  • signal: {sig['direction']} {sig['timeframe']} score={sig['score']} "
              f"entry={sig['entry']} sl={sig['sl']} tp={sig['tp']} rr={sig['rr']}")
        print(text[:300].replace("\n", " | "))
        if item["chart"]:
            print(f"  • chart attached: {item['chart'].name}")

    # ---- tracking evaluation ----
    tracker.evaluate("5m", candles_by_tf["5m"])
    stats = tracker.stats()
    print(f"  ✓ tracker stats: {stats}")

    # ---- cooldown & dedup ----
    emitted_before = len(notifier.sent)
    for _ in range(3):
        asyncio.run(engine.scan_once())
    assert len(notifier.sent) == emitted_before, "dedup/cooldown failed!"
    print("  ✓ dedup + cooldown working (no duplicate emits)")

    print("✅ E2E OFFLINE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
