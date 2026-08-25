"""Simulate tracked history and print the exact /stats, /signals, /status text."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from bot.config import Config
from bot.tracking import SignalTracker

cfg = Config.load()
tracker = SignalTracker(cfg.data_dir, enabled=True)
tracker.reset()

# Simulate ~2 weeks of plausible signals with realistic outcomes
rng = np.random.default_rng(5)
strategies_pool = [
    ["ema_pullback"],
    ["order_block", "ema_pullback"],
    ["vwap_reversion", "macd_momentum"],
    ["break_retest", "rsi_divergence"],
    ["macd_momentum", "order_block"],
    ["rsi_divergence", "vwap_reversion"],
]
tfs = ["1m", "5m", "15m"]
base = dt.datetime(2026, 8, 6, 8, 30, tzinfo=dt.timezone.utc)

for k in range(47):
    direction = "long" if rng.random() < 0.52 else "short"
    tf = tfs[k % 3]
    entry = float(round(3360 + rng.normal(0, 25), 2))
    atr = float(round(2.2 + rng.random() * 2.5, 2))
    if direction == "long":
        sl, tp = round(entry - 1.5 * atr, 2), round(entry + 2.5 * atr, 2)
    else:
        sl, tp = round(entry + 1.5 * atr, 2), round(entry - 2.5 * atr, 2)
    ts = int((base + dt.timedelta(hours=3.5 * k)).timestamp()) * 1000
    score = int(rng.integers(66, 98))
    strategies = strategies_pool[k % len(strategies_pool)]
    names = ", ".join(s.replace("_", " ").title() for s in strategies)
    reasons = [
        "Uptrend above EMA50, pullback reclaimed EMA21",
        "Volume above average",
        "Bullish order block 3358.10–3361.44 defended",
        "MACD crossed above signal with rising histogram",
    ][: int(rng.integers(1, 4))]
    roll = rng.random()
    status = "win" if roll < 0.62 else ("loss" if roll < 0.95 else "open")
    r_mult = round((tp - entry) / (entry - sl), 2) if direction == "long" else round((entry - tp) / (sl - entry), 2)
    tracker.record({
        "version": "1.0.0",
        "direction": direction,
        "timeframe": tf,
        "candle_ts": ts,
        "time_str": dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 2.5,
        "rr": 1.7,
        "atr": atr,
        "score": score,
        "strategies": strategies,
        "strategy_names": names,
        "reasons": reasons,
        "status": status if status != "open" else None,
        "r_multiple": r_mult if status == "win" else (-1.0 if status == "loss" else None),
    })

st = tracker.stats()
print("===== /stats =====")
pf = st["profit_factor"]
pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
print(
    "<b>📈 Tracking stats</b>\n"
    f"Total signals: {st['total']} (pending: {st['pending']})\n"
    f"Closed: {st['closed']} → ✅ wins {st['wins']} · ❌ losses {st['losses']}\n"
    f"Win rate: <b>{st['win_rate']:.1f}%</b>\n"
    f"Total P&L: <b>{st['total_r']:+}R</b> (avg {st['avg_r']:+.2f}R/trade)\n"
    f"Profit factor: {pf_s}\n"
)
by = tracker.by_strategy()
print("<b>By strategy</b>")
for name, b in sorted(by.items(), key=lambda x: -x[1]["n"]):
    print(f"• {name}: {b['n']} signals ({b['wins']}W/{b['losses']}L)")

print("\n===== /signals 5 =====")
lines = ["<b>🕐 Recent signals</b>"]
for s in tracker.recent(5):
    emoji = "🟢" if s["direction"] == "long" else "🔴"
    status = {"win": "✅", "loss": "❌", "open": "⏳", None: "⏳"}.get(s.get("status"), "⏳")
    lines.append(
        f"{emoji} {s['time_str']} · {s['timeframe']} · {s['entry']} "
        f"→ SL {s['sl']} / TP {s['tp']} · {s['score']}/100 {status}"
    )
print("\n".join(lines))

print("\n===== /status =====")
print(
    "<b>📊 Bot status</b>\n"
    "⏱ Uptime: 34h 12m 7s · scans: 8214\n"
    "🔌 Exchange: ✅ mexc · XAU/USDT:USDT · latency 42ms · last ok 14:32:56 UTC\n"
    "💎 Symbol: <code>XAU/USDT:USDT</code>\n"
    "📈 Timeframes:\n"
    "  • 1m: 500 candles, last 3s ago\n"
    "  • 5m: 500 candles, last 12s ago\n"
    "  • 15m: 500 candles, last 9s ago\n"
    "⚙️ Toggles: ✅ gold ✅ charts ✅ ai ✅ broadcast ✅ tracking ✅ errors ✅ news\n"
    "⏸ News blackout: None (signals allowed)\n"
    "🔥 Runtime: cooldown 10m · min score 65 · confluence 2\n"
    "🤖 AI: enabled"
)

print("\n===== /blackout =====")
print("✅ No active news blackout.\nWindows (UTC): 12:30–13:45, 13:30–14:45, 18:00–19:30")

tracker.reset()
