"""Signal tracking — records every notified setup and marks it win/loss
when price hits TP or SL on a later candle.  Results feed /stats and the
paper-performance summary.  Persisted to ``data/signals.json``.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("goldbot.tracking")


class SignalTracker:
    def __init__(self, data_dir: Path, enabled: bool = True):
        self.data_dir = data_dir
        self.enabled = enabled
        self.path = data_dir / "signals.json"
        self._signals: list[dict] = []
        self.load()

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if self.path.exists():
            try:
                self._signals = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Could not read %s: %s", self.path, exc)
                self._signals = []

    def save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._signals, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not save %s: %s", self.path, exc)

    # ------------------------------------------------------------------ #
    def record(self, signal: dict) -> None:
        if not self.enabled:
            return
        signal = dict(signal)
        signal.setdefault("recorded_at", time.time())
        self._signals.append(signal)
        # Keep the newest 2000
        self._signals = self._signals[-2000:]
        self.save()

    def reset(self) -> None:
        self._signals = []
        self.save()

    # ------------------------------------------------------------------ #
    def evaluate(self, timeframe: str, candles: list[list[float]]) -> None:
        """Mark open signals of this timeframe win/loss using new candles."""
        if not self.enabled or not self._signals:
            return
        changed = False
        for sig in self._signals:
            if sig.get("timeframe") != timeframe or sig.get("status") not in (None, "open"):
                continue
            sig_ts = sig.get("candle_ts", 0)
            direction = sig.get("direction")
            sl, tp = sig.get("sl"), sig.get("tp")
            if not sl or not tp:
                continue
            for candle in candles:
                ts, _, high, low, _, _ = candle[:6]
                if ts <= sig_ts:
                    continue
                if direction == "long":
                    if high >= tp:
                        sig["status"] = "win"
                        sig["closed_at"] = time.time()
                        sig["r_multiple"] = round((tp - sig["entry"]) / (sig["entry"] - sl), 2)
                        changed = True
                        break
                    if low <= sl:
                        sig["status"] = "loss"
                        sig["closed_at"] = time.time()
                        sig["r_multiple"] = -1.0
                        changed = True
                        break
                else:
                    if low <= tp:
                        sig["status"] = "win"
                        sig["closed_at"] = time.time()
                        sig["r_multiple"] = round((sig["entry"] - tp) / (sl - sig["entry"]), 2)
                        changed = True
                        break
                    if high >= sl:
                        sig["status"] = "loss"
                        sig["closed_at"] = time.time()
                        sig["r_multiple"] = -1.0
                        changed = True
                        break
        if changed:
            self.save()

    # ------------------------------------------------------------------ #
    def recent(self, n: int = 10) -> list[dict]:
        return list(reversed(self._signals[-n:]))

    def stats(self) -> dict:
        if not self._signals:
            return {"total": 0}
        closed = [s for s in self._signals if s.get("status") in ("win", "loss")]
        wins = [s for s in closed if s.get("status") == "win"]
        losses = [s for s in closed if s.get("status") == "loss"]
        pending = [s for s in self._signals if s.get("status") in (None, "open")]
        total_r = sum(float(s.get("r_multiple", 0.0)) for s in closed)
        profit_factor = (
            sum(float(s.get("r_multiple", 0.0)) for s in wins) / abs(sum(s.get("r_multiple", 0.0) for s in losses))
            if losses and sum(s.get("r_multiple", 0.0) for s in losses) != 0
            else float("inf") if wins else 0.0
        )
        return {
            "total": len(self._signals),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "pending": len(pending),
            "win_rate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
            "total_r": round(total_r, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else float("inf"),
            "avg_r": round(total_r / len(closed), 2) if closed else 0.0,
        }

    def by_strategy(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for s in self._signals:
            for name in s.get("strategies", []):
                bucket = out.setdefault(name, {"n": 0, "wins": 0, "losses": 0})
                bucket["n"] += 1
                if s.get("status") == "win":
                    bucket["wins"] += 1
                elif s.get("status") == "loss":
                    bucket["losses"] += 1
        return out
