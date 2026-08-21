"""Backtester — replays historical XAU/USDT candles through the exact same
strategy code used live and reports performance.

Usage:
    python -m bot.backtest --tf 5m --days 14 [--strategy all|ema_pullback,...] [--chart]

The engine and the backtester share :func:`bot.strategies` evaluation, so
results are representative of live behaviour (same confluence rules, same
SL/TP logic).  Use it to tune MIN_SIGNAL_SCORE, SL_ATR_MULT, TP_ATR_MULT,
strategy toggles etc. in your .env.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

import numpy as np

from bot.config import Config, ConfigError
from bot.engine import Params
from bot.indicators import IndicatorSet
from bot.logging_setup import setup_logging
from bot.state import AppState
from bot.strategies import STRATEGIES, ScanContext, merge_signals

log = logging.getLogger("goldbot.backtest")

TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


class Backtest:
    def __init__(self, cfg: Config, timeframe: str, days: int, strategies: list[str], out_dir: Path):
        self.cfg = cfg
        self.timeframe = timeframe
        self.days = days
        self.strategies = strategies
        self.out_dir = out_dir
        self.state = AppState()
        self.state.strategy_flags = {k: (k in strategies) for k in STRATEGIES}
        self.params = Params(cfg, self.state)

    # ------------------------------------------------------------------ #
    async def run(self) -> dict:
        from bot.exchange_client import ExchangeClient

        ex = ExchangeClient(self.cfg)
        await ex.connect()
        try:
            limit = min(self.cfg.max_candles * 4, (self.days * 1440) // TF_MINUTES[self.timeframe] + 200)
            log.info("Fetching %d candles of %s from %s...", limit, self.timeframe, ex.resolved_symbol)
            candles = await ex.fetch_ohlcv(self.timeframe, limit=limit)
            if len(candles) < 150:
                raise RuntimeError(f"Only {len(candles)} candles available — need at least 150")
            log.info("Got %d candles (%s → %s)",
                     len(candles),
                     dt.datetime.fromtimestamp(candles[0][0] / 1000, tz=dt.timezone.utc).date(),
                     dt.datetime.fromtimestamp(candles[-1][0] / 1000, tz=dt.timezone.utc).date())
            return self._run_on_candles(candles)
        finally:
            await ex.close()

    # ------------------------------------------------------------------ #
    def _run_on_candles(self, candles: list[list[float]]) -> dict:
        ind = IndicatorSet.compute(candles, self.params)
        n = len(candles)
        state: dict[str, dict] = {}

        trades: list[dict] = []
        open_trade: dict | None = None
        signals_found = 0

        for i in range(60, n - 1):  # last candle is forming in live data
            if not ind.ready(i):
                continue

            ctx = ScanContext(tf=self.timeframe, ind=ind, idx=i, params=self.params,
                              state=state.setdefault(self.timeframe, {}))
            signals = []
            for name, fn in STRATEGIES.items():
                if name not in self.strategies:
                    continue
                try:
                    sig = fn(ctx)
                except Exception as exc:
                    log.error("Strategy %s crashed at candle %d: %s", name, i, exc)
                    continue
                if sig is not None:
                    signals.append(sig)

            merged = merge_signals(
                signals,
                min_score=int(self.state.runtime.get("min_signal_score", self.cfg.min_signal_score)),
                min_confluence=int(self.state.runtime.get("min_confluence_strategies", self.cfg.min_confluence_strategies)),
            )

            # Close open trade if TP/SL hit on this candle
            if open_trade is not None:
                hi, lo = float(candles[i][2]), float(candles[i][3])
                if open_trade["direction"] == "long":
                    if hi >= open_trade["tp"]:
                        open_trade["result"] = "win"
                        open_trade["r"] = open_trade["rr"]
                        trades.append(open_trade)
                        open_trade = None
                    elif lo <= open_trade["sl"]:
                        open_trade["result"] = "loss"
                        open_trade["r"] = -1.0
                        trades.append(open_trade)
                        open_trade = None
                else:
                    if lo <= open_trade["tp"]:
                        open_trade["result"] = "win"
                        open_trade["r"] = open_trade["rr"]
                        trades.append(open_trade)
                        open_trade = None
                    elif hi >= open_trade["sl"]:
                        open_trade["result"] = "loss"
                        open_trade["r"] = -1.0
                        trades.append(open_trade)
                        open_trade = None

            if merged is None or open_trade is not None:
                continue

            signals_found += 1
            atr = float(ind.atr[i])
            if not np.isfinite(atr) or atr <= 0:
                continue
            entry = float(ind.close[i])
            sl_atr = float(self.state.runtime.get("sl_atr_mult", self.cfg.sl_atr_mult))
            tp_atr = float(self.state.runtime.get("tp_atr_mult", self.cfg.tp_atr_mult))
            if merged.direction == "long":
                sl, tp = entry - sl_atr * atr, entry + tp_atr * atr
            else:
                sl, tp = entry + sl_atr * atr, entry - tp_atr * atr
            open_trade = {
                "direction": merged.direction,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "rr": tp_atr / sl_atr,
                "score": merged.score,
                "strategies": [s.name for s in merged.signals],
                "candle_ts": int(candles[i][0]),
                "result": "open",
                "r": 0.0,
            }

        # Force-close a still-open trade at the last price
        if open_trade is not None:
            last_close = float(candles[-2][4])
            open_trade["result"] = "open"
            open_trade["r"] = 0.0
            trades.append(open_trade)

        return self._summarize(candles, trades, signals_found)

    # ------------------------------------------------------------------ #
    def _summarize(self, candles, trades: list[dict], signals_found: int) -> dict:
        closed = [t for t in trades if t["result"] in ("win", "loss")]
        wins = [t for t in closed if t["result"] == "win"]
        losses = [t for t in closed if t["result"] == "loss"]
        total_r = sum(t["r"] for t in closed)
        pf = (
            sum(t["r"] for t in wins) / abs(sum(t["r"] for t in losses))
            if losses and sum(t["r"] for t in losses) != 0
            else float("inf") if wins else 0.0
        )

        # Equity curve in R
        equity = [0.0]
        for t in closed:
            equity.append(equity[-1] + t["r"])
        peak = max(equity)
        max_dd = 0.0
        for v in equity:
            max_dd = max(max_dd, peak - v)
            peak = max(peak, v)

        # Per-strategy
        by_strategy: dict[str, dict] = {}
        for t in closed:
            for sname in t["strategies"]:
                b = by_strategy.setdefault(sname, {"n": 0, "wins": 0, "losses": 0, "r": 0.0})
                b["n"] += 1
                b["r"] += t["r"]
                if t["result"] == "win":
                    b["wins"] += 1
                else:
                    b["losses"] += 1

        summary = {
            "timeframe": self.timeframe,
            "candles": len(candles),
            "signals": signals_found,
            "trades": len(trades),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
            "total_r": round(total_r, 2),
            "profit_factor": round(pf, 2) if pf != float("inf") else float("inf"),
            "avg_r": round(total_r / len(closed), 2) if closed else 0.0,
            "max_drawdown_r": round(max_dd, 2),
            "by_strategy": by_strategy,
            "equity": equity,
            "params": {
                "sl_atr_mult": self.state.runtime["sl_atr_mult"],
                "tp_atr_mult": self.state.runtime["tp_atr_mult"],
                "min_score": self.state.runtime["min_signal_score"],
                "min_confluence": self.state.runtime["min_confluence_strategies"],
            },
        }
        return summary

    # ------------------------------------------------------------------ #
    def print_report(self, summary: dict) -> None:
        pf = summary["profit_factor"]
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        print("=" * 64)
        print(f"BACKTEST  XAU/USDT  {summary['timeframe']}  ({summary['candles']} candles)")
        print("=" * 64)
        print(f"Setups found          : {summary['signals']}")
        print(f"Trades taken          : {summary['trades']} (closed {summary['closed']})")
        print(f"Wins / Losses         : {summary['wins']} / {summary['losses']}")
        print(f"Win rate              : {summary['win_rate']:.1f}%")
        print(f"Total P&L             : {summary['total_r']:+.2f}R")
        print(f"Profit factor         : {pf_s}")
        print(f"Avg P&L per trade     : {summary['avg_r']:+.2f}R")
        print(f"Max drawdown          : {summary['max_drawdown_r']:.2f}R")
        print(f"Params (SL/TP/score/conf): {summary['params']}")
        if summary["by_strategy"]:
            print("-" * 64)
            print("By strategy (trades that closed):")
            for name, b in sorted(summary["by_strategy"].items(), key=lambda x: -x[1]["r"]):
                wr = b["wins"] / b["n"] * 100 if b["n"] else 0
                print(f"  {name:<20} n={b['n']:>3}  {b['wins']:>2}W/{b['losses']:<2}L  wr={wr:4.0f}%  P&L={b['r']:+.2f}R")
        print("=" * 64)

    def save_equity_chart(self, summary: dict, out_dir: Path) -> Path:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"backtest_{summary['timeframe']}.png"
            fig, ax = plt.subplots(figsize=(11, 5))
            ax.plot(summary["equity"], color="#26a69a", linewidth=1.5)
            ax.axhline(0, color="#ef5350", linewidth=0.8, linestyle="--")
            ax.set_title(f"XAU/USDT {summary['timeframe']} — equity curve ({summary['total_r']:+.1f}R)")
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            fig.savefig(path, dpi=110)
            plt.close(fig)
            return path
        except Exception as exc:
            log.warning("Equity chart failed: %s", exc)
            return out_dir / "none"


async def _main(args) -> int:
    setup_logging("INFO")
    try:
        cfg = Config.load()
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    strategies = ["all"]
    if args.strategy and args.strategy != "all":
        strategies = [s.strip().lower() for s in args.strategy.split(",") if s.strip()]
        unknown = [s for s in strategies if s not in STRATEGIES]
        if unknown:
            print(f"❌ Unknown strategies: {', '.join(unknown)}. Available: {', '.join(STRATEGIES)}", file=sys.stderr)
            return 1
        strategies = [s for s in strategies if cfg.strategy_enabled.get(s, True)]

    bt = Backtest(cfg, timeframe=args.tf, days=args.days, strategies=strategies, out_dir=cfg.data_dir)
    try:
        summary = await bt.run()
    except Exception as exc:
        print(f"❌ Backtest failed: {exc}", file=sys.stderr)
        return 1

    bt.print_report(summary)
    if args.chart:
        path = bt.save_equity_chart(summary, cfg.data_dir / "charts")
        print(f"Equity chart saved: {path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="XAU/USDT scalping backtest")
    parser.add_argument("--tf", default="5m", choices=list(TF_MINUTES), help="timeframe to test")
    parser.add_argument("--days", type=int, default=14, help="days of history (max ~60)")
    parser.add_argument("--strategy", default="all", help="comma-separated strategy names or 'all'")
    parser.add_argument("--chart", action="store_true", help="save equity curve PNG")
    args = parser.parse_args()
    code = asyncio.run(_main(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
