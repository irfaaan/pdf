"""Entry point.

Modes:
    python run.py                  — live bot (polling MEXC + Telegram)
    python run.py --once           — run a single scan cycle, then exit
    python run.py --smoke          — offline smoke test (synthetic candles)
    python run.py --selftest       — full connectivity self-test (Telegram + MEXC)
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import signal
import sys
import time

from telegram import Update
from telegram.ext import Application, ContextTypes

from bot.ai_advisor import AIAdvisor
from bot.commands import Deps, register_handlers
from bot.config import BASE_DIR, Config, ConfigError
from bot.engine import SignalEngine
from bot.exchange_client import ExchangeClient
from bot.logging_setup import setup_logging
from bot.notifier import Notifier
from bot.state import AppState
from bot.strategies import STRATEGIES
from bot.tracking import SignalTracker

log = logging.getLogger("goldbot.main")


# =========================================================================== #
# Offline smoke test — runs the full strategy stack on synthetic candles
# =========================================================================== #
def _synthetic_candles(n: int = 800, seed: int = 42) -> list[list[float]]:
    import numpy as np

    rng = np.random.default_rng(seed)
    price = 3400.0
    candles: list[list[float]] = []
    ts = int(time.time() * 1000) - n * 60_000
    drift = 0.0
    for i in range(n):
        # Alternate between trending and ranging regimes so strategies have something to see
        if i % 250 < 120:
            drift = 0.35
        elif i % 250 < 170:
            drift = -0.5
        else:
            drift = 0.0
        o = price
        c = price + drift + float(rng.normal(0, 1.6))
        h = max(o, c) + abs(float(rng.normal(0, 1.2)))
        l = min(o, c) - abs(float(rng.normal(0, 1.2)))
        v = float(rng.uniform(50, 400))
        candles.append([ts, o, h, l, c, v])
        price = c
        ts += 60_000
    return candles


def run_smoke(cfg: Config) -> int:
    """Offline check of config + indicators + strategies on synthetic data."""
    from bot.engine import Params
    from bot.indicators import IndicatorSet
    from bot.strategies import ScanContext, merge_signals

    print("🧪 Smoke test (offline, synthetic candles)...")
    candles = _synthetic_candles()
    state = AppState()
    params = Params(cfg, state)
    ind = IndicatorSet.compute(candles, params)
    assert ind.ready(300), "indicators not ready after warm-up"
    assert len(ind.ema_fast) == len(candles), "indicator length mismatch"

    tf_state: dict[str, dict] = {}
    found = 0
    for i in range(200, len(candles) - 1):
        ctx = ScanContext(tf="1m", ind=ind, idx=i, params=params, state=tf_state.setdefault("1m", {}))
        signals = []
        for name, fn in STRATEGIES.items():
            sig = fn(ctx)
            if sig is not None:
                signals.append(sig)
        merged = merge_signals(
            signals,
            min_score=int(state.runtime["min_signal_score"]),
            min_confluence=int(state.runtime["min_confluence_strategies"]),
        )
        if merged is not None:
            found += 1
            if found <= 3:
                print(f"  ✓ sample setup @candle {i}: {merged.direction.upper()} score={merged.score} "
                      f"strategies={[s.name for s in merged.signals]}")

    print(f"  ✓ {len(candles)} synthetic candles processed, {found} setups detected (synthetic data)")
    print("✅ Smoke test PASSED — strategy stack runs without errors")
    return 0


# =========================================================================== #
# Self-test — config, smoke, Telegram and exchange connectivity
# =========================================================================== #
async def run_selftest(cfg: Config) -> int:
    print("🧪 SELF-TEST")
    ok = True

    # 1. config
    print(f"  ✓ Config loaded: {cfg.summary()}")
    print(f"  ✓ Telegram token: {cfg.telegram_token[:10]}…  channel: {cfg.channel_id}")
    print(f"  ✓ AI: model={cfg.ai_model or '(none)'} keys={len(cfg.openrouter_keys)}")
    if cfg.ai_model:
        print(f"     (note: AI model will be verified at runtime — failures are non-fatal)")

    # 2. smoke
    run_smoke(cfg)

    # 3. Telegram
    print("  • Telegram: checking bot token via getMe...")
    from telegram import Bot
    from telegram.error import TelegramError

    bot = Bot(token=cfg.telegram_token)
    try:
        me = await bot.get_me()
        print(f"  ✓ Bot @{me.username} (id {me.id}) is valid")
    except TelegramError as exc:
        print(f"  ✗ Telegram getMe failed: {exc}")
        ok = False

    # 4. Exchange
    print(f"  • Exchange: connecting to {cfg.primary_exchange}...")
    ex = ExchangeClient(cfg)
    try:
        await ex.connect()
        print(f"  ✓ Connected, resolved symbol: {ex.resolved_symbol}")
        ticker = await ex.fetch_ticker()
        last = ticker.get("last") or ticker.get("close")
        print(f"  ✓ Ticker: XAU/USDT = {last}")
        candles = await ex.fetch_ohlcv(cfg.timeframes[0], limit=120)
        print(f"  ✓ OHLCV {cfg.timeframes[0]}: {len(candles)} candles, "
              f"last close = {candles[-1][4]}")
    except Exception as exc:
        print(f"  ✗ Exchange check failed: {exc}")
        ok = False
    finally:
        await ex.close()

    # 5. Test message to the channel
    print(f"  • Posting test message to {cfg.channel_id}...")
    try:
        msg = await bot.send_message(
            chat_id=cfg.channel_id,
            text="🧪 <b>Self-test OK</b> — bot is connected and posting to this channel.",
            parse_mode="HTML",
        )
        print(f"  ✓ Test message posted (message id {msg.message_id})")
    except TelegramError as exc:
        print(f"  ✗ Could not post to channel: {exc}")
        print("    → Make sure the bot is added as an ADMIN to the channel/group!")
        ok = False

    print("✅ SELF-TEST PASSED" if ok else "❌ SELF-TEST FAILED — fix the issues above")
    return 0 if ok else 1


# =========================================================================== #
# Live mode
# =========================================================================== #
async def engine_loop(engine: SignalEngine, state: AppState, stop: asyncio.Event) -> None:
    poll = float(state.runtime.get("poll_interval_seconds", 15.0))
    while not stop.is_set():
        try:
            await engine.scan_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Engine scan crashed: %s", exc)
            await engine._maybe_notify_error(f"Engine scan crashed: {exc}")
        poll = float(state.runtime.get("poll_interval_seconds", 15.0))
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll)
        except asyncio.TimeoutError:
            pass


async def watchdog(engine: SignalEngine, state: AppState, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        else:
            return
        stale_after = max(120.0, float(state.runtime.get("poll_interval_seconds", 15.0)) * 4)
        if state.last_scan_at and time.time() - state.last_scan_at > stale_after:
            log.warning("Watchdog: engine heartbeat stale (last scan %.0fs ago)", time.time() - state.last_scan_at)
            await engine._maybe_notify_error(
                f"Engine heartbeat stale — last successful scan was "
                f"{int(time.time() - state.last_scan_at)}s ago. Exchange may be unreachable."
            )


async def _tg_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Telegram update error: %s", context.error)
    try:
        deps: Deps = context.bot_data.get("deps")
        if deps:
            await deps.notifier.notify_error(f"Telegram handler error: {context.error}")
    except Exception:
        pass


async def run_live(cfg: Config, once: bool = False) -> int:
    state = AppState()
    state.strategy_flags = dict(cfg.strategy_enabled)

    # --- Telegram ---
    app = Application.builder().token(cfg.telegram_token).build()
    try:
        # Note: Application.initialize() itself calls get_me(), so a bad token
        # or unreachable api.telegram.org surfaces here.
        await app.initialize()
        me = await app.bot.get_me()
        log.info("Telegram bot @%s connected", me.username)
    except Exception as exc:
        log.error("Telegram token invalid or network unreachable: %s", exc)
        print("❌ Cannot reach Telegram — check TELEGRAM_TOKEN and internet access.", file=sys.stderr)
        return 1

    notifier = Notifier(app.bot, cfg)
    tracker = SignalTracker(cfg.data_dir, enabled=cfg.tracking_enabled)
    ai = AIAdvisor(cfg)
    deps = Deps(cfg=cfg, state=state, engine=None, tracker=tracker, notifier=notifier,
                exchange=None, ai=ai)
    app.bot_data["deps"] = deps
    register_handlers(app, deps)
    app.add_error_handler(_tg_error_handler)

    # --- Exchange ---
    ex = ExchangeClient(cfg)
    try:
        await ex.connect()
    except Exception as exc:
        log.error("Exchange connect failed: %s", exc)
        print(f"❌ Cannot connect to {cfg.primary_exchange}: {exc}", file=sys.stderr)
        print("   Check that GOLD_SYMBOL is listed on MEXC (XAU/USDT:USDT swap or XAU/USDT spot).",
              file=sys.stderr)
        await app.shutdown()
        return 1

    deps.exchange = ex
    engine = SignalEngine(cfg, ex, notifier, tracker, ai, state)
    deps.engine = engine

    # --- start polling ---
    await app.updater.start_polling(drop_pending_updates=True)
    await app.start()
    log.info("Bot started — listening for commands, scanning %s on %s",
             ",".join(cfg.timeframes), cfg.gold_symbol)

    # Signal handlers for graceful shutdown
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    if once:
        await engine.scan_once()
        print(f"✅ Single scan done: {state.scan_count} scan(s), "
              f"last signal: {state.last_signal and state.last_signal.get('direction') or 'none'}")
    else:
        engine_task = asyncio.create_task(engine_loop(engine, state, stop))
        watchdog_task = asyncio.create_task(watchdog(engine, state, stop))
        try:
            await stop.wait()
            log.info("Shutdown signal received")
        finally:
            engine_task.cancel()
            watchdog_task.cancel()
            for task in (engine_task, watchdog_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # --- cleanup ---
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:
        pass
    await ex.close()
    log.info("Bot stopped cleanly")
    return 0


# =========================================================================== #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gold-bot", description="XAU/USDT scalping bot (MEXC → Telegram)")
    parser.add_argument("--once", action="store_true", help="run a single scan cycle and exit")
    parser.add_argument("--smoke", action="store_true", help="offline smoke test on synthetic data")
    parser.add_argument("--selftest", action="store_true", help="full self-test (Telegram + MEXC connectivity)")
    args = parser.parse_args(argv)

    log = setup_logging("INFO")
    try:
        cfg = Config.load()
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    log.info("Config: %s", cfg.summary())
    if cfg.meme_enabled:
        log.info("MEME_ENABLED=true found in .env — this build is gold-only (XAU/USDT); meme module ignored.")

    if args.smoke:
        return run_smoke(cfg)
    if args.selftest:
        return asyncio.run(run_selftest(cfg))

    return asyncio.run(run_live(cfg, once=args.once))


if __name__ == "__main__":
    sys.exit(main())
