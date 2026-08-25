"""MEXC exchange client (ccxt async) with retries, health tracking and
symbol fallback (swap -> spot).  Public market data is used, so API keys
are optional for this bot.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import ccxt.async_support as ccxt

log = logging.getLogger("goldbot.exchange")

RETRYABLE = (
    ccxt.NetworkError,
    ccxt.ExchangeNotAvailable,
    ccxt.RequestTimeout,
    ccxt.DDoSProtection,
    ccxt.ExchangeError,
    ccxt.OperationFailed,
)

MAX_RETRIES = 5
BACKOFF_BASE = 2.0


class ExchangeClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exchange: ccxt.Exchange | None = None
        self.symbol: str = cfg.gold_symbol
        self.resolved_symbol: str | None = None
        self.last_ok: float = 0.0
        self.last_error: str = ""
        self.error_count: int = 0
        self._lock = asyncio.Lock()
        self._markets_loaded = False

    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """Create the ccxt client, try swap first, fall back to spot."""
        ex_name = self.cfg.primary_exchange
        klass = getattr(ccxt, ex_name, None)
        if klass is None:
            raise RuntimeError(f"ccxt has no exchange class '{ex_name}'")

        self.exchange = klass({
            "apiKey": self.cfg.mexc_api_key or None,
            "secret": self.cfg.mexc_api_secret or None,
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {"defaultType": "swap"},
        })

        candidates = [self.cfg.gold_symbol]
        if self.cfg.gold_symbol != "XAU/USDT:USDT":
            candidates.append("XAU/USDT:USDT")
        if not self.cfg.gold_symbol.endswith(":USDT"):
            candidates.append("XAU/USDT")
        candidates = list(dict.fromkeys(candidates))

        await self._load_markets_with_retry(candidates)

    async def _load_markets_with_retry(self, candidates: list[str]) -> None:
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                await self.exchange.load_markets()
                break
            except RETRYABLE as exc:
                attempt += 1
                wait = BACKOFF_BASE ** attempt
                log.warning("load_markets failed (%s), retry %d/%d in %.0fs", exc, attempt, MAX_RETRIES, wait)
                if attempt >= MAX_RETRIES:
                    raise
                await asyncio.sleep(wait)

        for cand in candidates:
            if cand in self.exchange.markets:
                self.resolved_symbol = cand
                break

        if self.resolved_symbol is None:
            # Try loading symbol explicitly (some exchanges need it)
            for cand in candidates:
                try:
                    market = self.exchange.market(cand)
                    self.resolved_symbol = market["symbol"]
                    break
                except Exception:
                    continue

        if self.resolved_symbol is None:
            available = [s for s in self.exchange.markets if "XAU" in s]
            raise RuntimeError(
                f"Symbol {self.cfg.gold_symbol} not found on {self.cfg.primary_exchange}. "
                f"XAU markets seen: {available[:5]}"
            )
        self._markets_loaded = True
        log.info("Connected to %s, resolved symbol: %s", self.cfg.primary_exchange, self.resolved_symbol)

    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        if self.exchange is not None:
            try:
                await self.exchange.close()
            except Exception:
                pass

    async def _call(self, method: str, *args, **kwargs) -> Any:
        """Run a ccxt call with retry/backoff, guarded by a lock."""
        async with self._lock:
            attempt = 0
            while True:
                try:
                    result = await getattr(self.exchange, method)(*args, **kwargs)
                    self.last_ok = time.time()
                    self.error_count = 0
                    return result
                except RETRYABLE as exc:
                    attempt += 1
                    self.error_count += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if attempt >= MAX_RETRIES:
                        log.error("Call %s failed after %d retries: %s", method, MAX_RETRIES, self.last_error)
                        raise
                    wait = min(BACKOFF_BASE ** attempt, 30.0)
                    log.warning("Call %s failed (%s) — retry %d/%d in %.0fs",
                                method, self.last_error, attempt, MAX_RETRIES, wait)
                    await asyncio.sleep(wait)

    # ------------------------------------------------------------------ #
    async def fetch_ohlcv(self, timeframe: str, limit: int) -> list[list[float]]:
        if not self.resolved_symbol:
            raise RuntimeError("Exchange not connected")
        candles = await self._call("fetch_ohlcv", self.resolved_symbol, timeframe, limit=limit)
        # ccxt returns [ts, o, h, l, c, v]; keep only what we need
        return [list(map(float, c)) for c in candles]

    async def fetch_ticker(self) -> dict:
        if not self.resolved_symbol:
            raise RuntimeError("Exchange not connected")
        return await self._call("fetch_ticker", self.resolved_symbol)

    async def fetch_order_book(self, limit: int = 25) -> dict:
        if not self.resolved_symbol:
            raise RuntimeError("Exchange not connected")
        return await self._call("fetch_order_book", self.resolved_symbol, limit)

    async def fetch_funding_rate(self) -> dict | None:
        if not self.resolved_symbol:
            return None
        try:
            return await self._call("fetch_funding_rate", self.resolved_symbol)
        except Exception as exc:  # funding rate is informational only
            log.debug("fetch_funding_rate failed: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    @property
    def healthy(self) -> bool:
        if not self.resolved_symbol or self.last_ok == 0.0:
            return False
        return (time.time() - self.last_ok) < 300  # fresh within 5 minutes

    def latency_ms(self) -> float | None:
        if not self.exchange or not self.exchange.last_response_headers:
            return None
        try:
            return float(self.exchange.latency) * 1000.0
        except Exception:
            return None

    def status_text(self) -> str:
        if not self.resolved_symbol:
            return "❌ not connected"
        lat = self.latency_ms()
        lat_s = f"{lat:.0f}ms" if lat is not None else "n/a"
        last = time.strftime("%H:%M:%S", time.localtime(self.last_ok)) if self.last_ok else "never"
        state = "✅" if self.healthy else "⚠️ stale"
        return f"{state} {self.cfg.primary_exchange} · {self.resolved_symbol} · latency {lat_s} · last ok {last} UTC"
