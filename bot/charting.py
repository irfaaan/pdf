"""Candlestick chart rendering (matplotlib, Agg backend).

Produces a dark-themed PNG with EMA lines, VWAP, Bollinger bands, the
signal's entry / stop / target levels and a volume panel — used for signal
notifications and the /chart command.
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

log = logging.getLogger("goldbot.charting")

GREEN = "#26a69a"
RED = "#ef5350"
BG = "#0d1117"
GRID = "#21262d"
TEXT = "#e6edf3"
BLUE = "#58a6ff"
ORANGE = "#f0883e"


def _price_fmt(price: float) -> str:
    if price >= 1000:
        return f"{price:.1f}"
    if price >= 100:
        return f"{price:.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.6f}"


def render_chart(
    candles,
    ind,
    *,
    title: str,
    direction: str | None = None,
    entry: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    signal_index: int | None = None,
    out_path: Path,
) -> Path:
    """Render a chart PNG. `candles` are ccxt OHLCV rows, `ind` an IndicatorSet."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    arr = np.asarray(candles, dtype=float)
    ts = arr[:, 0].astype(np.int64)
    o, h, l, c, v = (arr[:, i] for i in range(1, 6))
    n = len(c)

    start = max(0, n - 160)
    idx = np.arange(start, n)
    x = np.arange(len(idx))

    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(12, 7.5), sharex=True,
        gridspec_kw={"height_ratios": [4, 1]},
        facecolor=BG,
    )
    for ax in (ax_price, ax_vol):
        ax.set_facecolor(BG)
        ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
        ax.tick_params(colors=TEXT, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(GRID)

    # Candles
    for k, i in enumerate(idx):
        color = GREEN if c[i] >= o[i] else RED
        wick_color = color
        ax_price.plot([k, k], [l[i], h[i]], color=wick_color, linewidth=1, zorder=2)
        body_lo, body_hi = min(o[i], c[i]), max(o[i], c[i])
        if body_hi - body_lo < 1e-12:
            body_hi = body_lo + (h[i] - l[i]) * 0.02 or 1e-9
        ax_price.add_patch(Rectangle(
            (k - 0.32, body_lo), 0.64, body_hi - body_lo,
            facecolor=color, edgecolor=color, linewidth=0.5, zorder=3,
        ))

    # Indicators
    def plot_series(values, color, label, width=1.2):
        seg = values[start:n]
        finite = np.isfinite(seg)
        ax_price.plot(x[finite], seg[finite], color=color, linewidth=width, label=label, zorder=4)

    plot_series(ind.ema_fast, "#f1c40f", "EMA9")
    plot_series(ind.ema_mid, ORANGE, "EMA21")
    plot_series(ind.ema_slow, "#c792ea", "EMA50")
    plot_series(ind.vwap, BLUE, "VWAP")
    plot_series(ind.bb_up, "#3d444d", None, width=0.8)
    plot_series(ind.bb_low, "#3d444d", None, width=0.8)

    # Signal levels
    if direction and entry is not None:
        ecolor = GREEN if direction == "long" else RED
        label = "LONG" if direction == "long" else "SHORT"
        ax_price.axhline(entry, color=ecolor, linewidth=1.4, linestyle="--", alpha=0.9, label=f"{label} entry")
        if sl is not None:
            ax_price.axhline(sl, color=RED if direction == "long" else GREEN, linewidth=1.2,
                             linestyle=":", alpha=0.9, label="SL")
        if tp is not None:
            ax_price.axhline(tp, color=GREEN if direction == "long" else RED, linewidth=1.2,
                             linestyle=":", alpha=0.9, label="TP")
        if signal_index is not None and start <= signal_index < n:
            k = signal_index - start
            ax_price.scatter([k], [entry], marker="^" if direction == "long" else "v",
                             color=ecolor, s=180, zorder=6, edgecolors="white", linewidths=0.8)

    # Volume
    for k, i in enumerate(idx):
        color = GREEN if c[i] >= o[i] else RED
        ax_vol.bar(k, v[i], width=0.64, color=color, alpha=0.7)
    if ind.vol_sma is not None:
        seg = ind.vol_sma[start:n]
        finite = np.isfinite(seg)
        ax_vol.plot(x[finite], seg[finite], color=BLUE, linewidth=1.0, label="vol SMA")

    ax_price.set_title(title, color=TEXT, fontsize=12, loc="left", pad=10)
    ax_price.legend(loc="upper left", fontsize=8, facecolor=BG, edgecolor=GRID, labelcolor=TEXT, ncol=3)
    ax_vol.set_ylabel("Volume", color=TEXT, fontsize=9)

    # X axis: time labels
    step = max(1, len(idx) // 8)
    ticks = list(range(0, len(idx), step))
    tick_labels = [_fmt_ts(ts[start + k]) for k in ticks]
    ax_vol.set_xticks(ticks)
    ax_vol.set_xticklabels(tick_labels, color=TEXT, fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, dpi=110, facecolor=BG)
    plt.close(fig)
    log.info("Chart saved: %s", out_path)
    return out_path


def _fmt_ts(ts_ns: int) -> str:
    import datetime

    dt = datetime.datetime.fromtimestamp(ts_ns / 1000, tz=datetime.timezone.utc)
    return dt.strftime("%m-%d %H:%M")


def format_price(price: float) -> str:
    return _price_fmt(price)
