"""Telegram command handlers.

Commands:
    /start, /help          — info
    /status                — bot & exchange health, toggles, uptime
    /price [tf]            — current XAU/USDT price (+ optional chart)
    /signals [n]           — last n signals
    /stats                 — tracked win/loss performance
    /chart [tf]            — render a chart of the latest candles
    /test                  — send a test message + chart to the channel
    /enable <module>       — turn a module on (gold, charts, ai, broadcast, tracking, errors, news)
    /disable <module>      — turn a module off
    /set <param> <value>   — change a runtime parameter live
    /cooldown <minutes>    — shortcut for /set signal_cooldown_minutes
    /strategy <name> on|off— enable/disable a strategy live
    /strategies            — list strategies and their state
    /reset                 — wipe tracked signals
    /blackout              — current news blackout status

Mutating commands (/set, /enable, /disable, /strategy, /cooldown, /reset)
require ADMIN_CHAT_ID when it is configured; otherwise anyone can use them.
(All angle-bracket placeholders in this text are escaped with &lt; &gt; so
Telegram's HTML parser accepts the message.)
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.charting import format_price, render_chart
from bot.state import RUNTIME_PARAMS

log = logging.getLogger("goldbot.commands")

HELP_TEXT = """\
<b>🤖 Gold Scalper Bot — XAU/USDT (MEXC)</b>

Scans 1m/5m/15m candles 24/7 and posts scalping setups to this channel.

<b>Commands</b>
/status — bot &amp; exchange health
/price — current gold price
/chart — latest candles chart
/signals — last signals
/stats — tracked win/loss stats
/test — test notification
/blackout — news blackout status
/lastsignal — reprint last setup
/aicheck — AI verdict on current market

<b>Admin commands</b>
/enable &lt;module&gt; · /disable &lt;module&gt;
  modules: gold, charts, ai, broadcast, tracking, errors, news
/set &lt;param&gt; &lt;value&gt; — live tuning
/cooldown &lt;minutes&gt;
/strategy &lt;name&gt; on|off — live strategy toggles
/strategies — list strategy states
/reset — wipe tracked signals

<b>Strategies</b>
ema_pullback · vwap_reversion · break_retest
order_block · macd_momentum · rsi_divergence
"""

MODULES = ("gold", "charts", "ai", "broadcast", "tracking", "errors", "news")


@dataclass
class Deps:
    cfg: object
    state: object
    engine: object
    tracker: object
    notifier: object
    exchange: object
    ai: object = None


def register_handlers(app: Application, deps: Deps) -> None:
    c = CommandHandler
    app.add_handler(c("start", cmd_start))
    app.add_handler(c("help", cmd_help))
    app.add_handler(c("status", cmd_status))
    app.add_handler(c("price", cmd_price))
    app.add_handler(c("chart", cmd_chart))
    app.add_handler(c("signals", cmd_signals))
    app.add_handler(c("stats", cmd_stats))
    app.add_handler(c("test", cmd_test))
    app.add_handler(c("blackout", cmd_blackout))
    app.add_handler(c("enable", cmd_enable))
    app.add_handler(c("disable", cmd_disable))
    app.add_handler(c("set", cmd_set))
    app.add_handler(c("cooldown", cmd_cooldown))
    app.add_handler(c("strategy", cmd_strategy))
    app.add_handler(c("strategies", cmd_strategies))
    app.add_handler(c("reset", cmd_reset))
    app.add_handler(c("lastsignal", cmd_last_signal))
    app.add_handler(c("aicheck", cmd_aicheck))


# --------------------------------------------------------------------------- #
def _is_admin(update: Update, deps: Deps) -> bool:
    if not deps.cfg.admin_chat_id:
        return True  # single-user setup: allow everything
    try:
        chat_id = str(update.effective_chat.id)
    except Exception:
        return False
    return chat_id == str(deps.cfg.admin_chat_id)


async def _reply(update: Update, text: str) -> None:
    """Send a reply. Tries HTML first; if Telegram rejects the markup
    (bad entities), falls back to plain text so the command still works."""
    if update.message is None:
        return
    try:
        await update.message.reply_text(text, parse_mode="HTML")
    except TelegramError as exc:
        log.warning("HTML reply failed (%s) — retrying as plain text", exc)
        try:
            await update.message.reply_text(text)
        except Exception as exc2:
            log.warning("plain-text reply also failed: %s", exc2)
    except Exception as exc:
        log.warning("reply failed: %s", exc)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    s, cfg, ex = deps.state, deps.cfg, deps.exchange
    up = s.uptime_seconds
    hh, mm, ss = int(up // 3600), int(up % 3600 // 60), int(up % 60)

    tf_lines = []
    for tf in cfg.timeframes:
        candles = deps.engine.latest_candles(tf)
        if candles:
            last_ts = int(candles[-1][0]) / 1000
            import datetime as dt

            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromtimestamp(last_ts, tz=dt.timezone.utc)
            tf_lines.append(f"  • {tf}: {len(candles)} candles, last {age.seconds}s ago")
        else:
            tf_lines.append(f"  • {tf}: not fetched yet")

    toggles = "  ".join(f"{'✅' if v else '⛔'} {k}" for k, v in s.toggles.items())
    text = (
        "<b>📊 Bot status</b>\n"
        f"⏱ Uptime: {hh}h {mm}m {ss}s · scans: {s.scan_count}\n"
        f"🔌 Exchange: {ex.status_text()}\n"
        f"💎 Symbol: <code>{cfg.gold_symbol}</code>\n"
        f"📈 Timeframes:\n" + "\n".join(tf_lines) + "\n"
        f"⚙️ Toggles: {toggles}\n"
        f"⏸ News blackout: {deps.engine.blackout_status()}\n"
        f"🔥 Runtime: cooldown {s.runtime['signal_cooldown_minutes']}m · "
        f"min score {s.runtime['min_signal_score']} · "
        f"confluence {s.runtime['min_confluence_strategies']}\n"
        f"🤖 AI: {'enabled' if s.toggles.get('ai', deps.cfg.ai_enabled) else 'disabled'}"
    )
    await _reply(update, text)


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    price = await deps.engine.current_price()
    if price is None:
        await _reply(update, "❌ Could not fetch price (exchange unreachable).")
        return
    args = context.args
    tf = args[0] if args and args[0] in deps.cfg.timeframes else deps.cfg.timeframes[0]
    candles = deps.engine.latest_candles(tf)
    await _reply(update, f"💎 <b>XAU/USDT</b> · <code>{format_price(price)}</code> USDT\n📈 Last {tf} candles: {len(candles) if candles else 0}")


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    tf = context.args[0] if context.args and context.args[0] in deps.cfg.timeframes else deps.cfg.timeframes[-1]
    candles = deps.engine.latest_candles(tf)
    if not candles:
        await _reply(update, f"❌ No candles cached for {tf} yet.")
        return
    try:
        from bot.indicators import IndicatorSet

        ind = IndicatorSet.compute(candles, deps.engine.params)
        path = render_chart(
            candles, ind,
            title=f"XAU/USDT {tf} — latest {len(candles)} candles",
            out_path=deps.cfg.data_dir / "charts" / f"cmd_chart_{tf}.png",
        )
        with open(path, "rb") as fh:
            await update.message.reply_photo(photo=fh)
    except Exception as exc:
        log.error("cmd_chart failed: %s", exc)
        await _reply(update, f"❌ Chart failed: {html.escape(str(exc))}")


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    n = 5
    if context.args and context.args[0].isdigit():
        n = min(int(context.args[0]), 20)
    recent = deps.tracker.recent(n)
    if not recent:
        await _reply(update, "No signals recorded yet.")
        return
    lines = ["<b>🕐 Recent signals</b>"]
    for s in recent:
        emoji = "🟢" if s["direction"] == "long" else "🔴"
        status = {"win": "✅", "loss": "❌", "open": "⏳", None: "⏳"}.get(s.get("status"), "⏳")
        lines.append(
            f"{emoji} {s['time_str']} · {s['timeframe']} · {format_price(s['entry'])} "
            f"→ SL {format_price(s['sl'])} / TP {format_price(s['tp'])} · {s['score']}/100 {status}"
        )
    await _reply(update, "\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    st = deps.tracker.stats()
    if st["total"] == 0:
        await _reply(update, "No tracked signals yet. Enable TRACKING_ENABLED and wait for setups.")
        return
    pf = st["profit_factor"]
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
    text = (
        "<b>📈 Tracking stats</b>\n"
        f"Total signals: {st['total']} (pending: {st['pending']})\n"
        f"Closed: {st['closed']} → ✅ wins {st['wins']} · ❌ losses {st['losses']}\n"
        f"Win rate: <b>{st['win_rate']:.1f}%</b>\n"
        f"Total P&L: <b>{st['total_r']:+}R</b> (avg {st['avg_r']:+.2f}R/trade)\n"
        f"Profit factor: {pf_s}\n"
    )
    by = deps.tracker.by_strategy()
    if by:
        text += "\n<b>By strategy</b>\n"
        for name, b in sorted(by.items(), key=lambda x: -x[1]["n"]):
            text += f"• {name}: {b['n']} signals ({b['wins']}W/{b['losses']}L)\n"
    await _reply(update, text)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    text = (
        "<b>✅ Test notification</b>\n"
        f"Bot is alive and posting to {html.escape(deps.cfg.chat_id)}\n"
        f"⏰ {now} UTC\n"
        f"⚙️ {deps.cfg.summary()}"
    )
    msg = await deps.notifier.send_text(text)
    if msg is None:
        await _reply(update, "❌ Could not post to the channel — check that the bot is admin of the channel!")
    else:
        await _reply(update, "✅ Test message posted to the channel.")

    # Also try a chart
    tf = deps.cfg.timeframes[0]
    candles = deps.engine.latest_candles(tf)
    if candles:
        try:
            from bot.indicators import IndicatorSet

            ind = IndicatorSet.compute(candles, deps.engine.params)
            path = render_chart(
                candles, ind,
                title=f"XAU/USDT {tf} — test chart",
                out_path=deps.cfg.data_dir / "charts" / "test_chart.png",
            )
            await deps.notifier.send_photo(path, "🧪 Test chart")
        except Exception as exc:
            log.error("test chart failed: %s", exc)


async def cmd_blackout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    note = deps.engine._in_blackout()
    if note:
        await _reply(update, f"⏸ {note} — new signals are suppressed until it ends.")
    else:
        windows = ", ".join(f"{a}–{b}" for a, b in deps.cfg.news_blackout_windows)
        await _reply(update, f"✅ No active news blackout.\nWindows (UTC): {windows}")


async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    if not _is_admin(update, deps):
        await _reply(update, "⛔ Not allowed.")
        return
    if not context.args or context.args[0].lower() not in MODULES:
        await _reply(update, f"Usage: /enable {'|'.join(MODULES)}")
        return
    mod = context.args[0].lower()
    deps.state.toggle(mod, True)
    await _reply(update, f"✅ Module <b>{mod}</b> enabled.")


async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    if not _is_admin(update, deps):
        await _reply(update, "⛔ Not allowed.")
        return
    if not context.args or context.args[0].lower() not in MODULES:
        await _reply(update, f"Usage: /disable {'|'.join(MODULES)}")
        return
    mod = context.args[0].lower()
    deps.state.toggle(mod, False)
    await _reply(update, f"⛔ Module <b>{mod}</b> disabled.")


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    if not _is_admin(update, deps):
        await _reply(update, "⛔ Not allowed.")
        return
    if len(context.args) < 2:
        params = "\n".join(f"• {k} — {v[1]} (default {v[0]})" for k, v in RUNTIME_PARAMS.items())
        await _reply(update, f"Usage: /set &lt;param&gt; &lt;value&gt;\n\n<b>Available params</b>\n{params}")
        return
    key, raw = context.args[0].lower(), context.args[1]
    if key not in RUNTIME_PARAMS:
        await _reply(update, f"❌ Unknown param '{key}'. Use /set alone to list them.")
        return
    try:
        value = float(raw)
        value = int(value) if isinstance(RUNTIME_PARAMS[key][0], int) else value
    except ValueError:
        await _reply(update, f"❌ '{raw}' is not a number.")
        return
    if deps.state.set_param(key, value):
        await _reply(update, f"✅ <b>{key}</b> = {value}")
    else:
        await _reply(update, f"❌ Value out of range for {key}.")


async def cmd_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    if not _is_admin(update, deps):
        await _reply(update, "⛔ Not allowed.")
        return
    if not context.args:
        await _reply(update, f"Current cooldown: {deps.state.runtime['signal_cooldown_minutes']}m. Usage: /cooldown &lt;minutes&gt;")
        return
    try:
        minutes = float(context.args[0])
    except ValueError:
        await _reply(update, "❌ Not a number.")
        return
    if deps.state.set_param("signal_cooldown_minutes", minutes):
        await _reply(update, f"✅ Signal cooldown = {minutes} minutes.")
    else:
        await _reply(update, "❌ Must be between 0 and 1440.")


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    if not _is_admin(update, deps):
        await _reply(update, "⛔ Not allowed.")
        return
    from bot.strategies import STRATEGY_NAMES

    if len(context.args) < 2 or context.args[0].lower() not in STRATEGY_NAMES:
        await _reply(update, f"Usage: /strategy &lt;name&gt; on|off\nNames: {', '.join(STRATEGY_NAMES)}")
        return
    name = context.args[0].lower()
    flag = context.args[1].lower()
    if flag not in ("on", "off"):
        await _reply(update, "❌ Use 'on' or 'off'.")
        return
    deps.state.strategy_flags[name] = flag == "on"
    await _reply(update, f"✅ Strategy <b>{STRATEGY_NAMES[name]}</b> {'enabled' if flag == 'on' else 'disabled'}.")


async def cmd_strategies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    from bot.strategies import STRATEGY_NAMES

    lines = ["<b>🧩 Strategies</b>"]
    for name, label in STRATEGY_NAMES.items():
        enabled = deps.state.strategy_flags.get(name, deps.cfg.strategy_enabled.get(name, True))
        lines.append(f"{'✅' if enabled else '⛔'} {label} ({name})")
    await _reply(update, "\n".join(lines))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    if not _is_admin(update, deps):
        await _reply(update, "⛔ Not allowed.")
        return
    deps.tracker.reset()
    await _reply(update, "✅ Tracking history wiped.")


async def cmd_last_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps: Deps = context.bot_data["deps"]
    if not deps.state.last_signal:
        await _reply(update, "No signal emitted yet.")
        return
    text = deps.notifier.build_signal_text(deps.state.last_signal)
    await _reply(update, text)


async def cmd_aicheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the configured AI model for its exact verdict on the current market."""
    deps: Deps = context.bot_data["deps"]
    if not deps.state.toggles.get("ai", deps.cfg.ai_enabled):
        await _reply(update, "🤖 AI vetting is disabled. Enable it: /enable ai")
        return
    if not deps.cfg.ai_model or not deps.cfg.openrouter_keys:
        await _reply(update, "❌ AI not configured — set AI_MODEL and OPENROUTER_API_KEY in .env")
        return

    await _reply(update, f"🤖 Asking <b>{html.escape(deps.cfg.ai_model)}</b> for its verdict…")

    # Build context from the latest cached candles
    price: float | None = None
    atr_val: float = 0.0
    try:
        tf = deps.cfg.timeframes[-1]
        candles = deps.engine.latest_candles(tf)
        if candles:
            from bot.indicators import atr as atr_ind

            tail = candles[-80:]
            price = float(tail[-1][4])
            atr_val = float(atr_ind([c[2] for c in tail], [c[3] for c in tail],
                                    [c[4] for c in tail], 14)[-1] or 0.0)
        if price is None:
            price = await deps.engine.current_price()
    except Exception as exc:
        log.warning("aicheck context failed: %s", exc)
    if price is None:
        await _reply(update, "❌ Could not fetch price — is the exchange connected?")
        return

    snapshot = (
        f"XAU/USDT price {price:.2f}, ATR(14) {atr_val:.2f}. "
        "Give your exact one-line scalping bias for the next 15 minutes: "
        "CONFIRM (long), REJECT, or NEUTRAL with a short reason."
    )
    verdict = await deps.ai.vet(snapshot, price, atr_val)
    if verdict is None:
        await _reply(update, "🤖 AI returned nothing usable (see logs). The model/API may be unreachable.")
        return
    await _reply(update, f"🤖 <b>AI verdict</b> ({html.escape(deps.cfg.ai_model)}):\n<code>{html.escape(verdict)}</code>")
