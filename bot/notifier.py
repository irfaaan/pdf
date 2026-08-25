"""Telegram notifier — posts signals, charts and status messages.

Uses the shared python-telegram-bot Application (same bot handles commands
and channel posting).  All sends are guarded by try/except and logged so a
transient Telegram hiccup never takes the engine down.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from bot.charting import format_price

log = logging.getLogger("goldbot.notifier")

EMOJI = {"long": "🟢", "short": "🔴"}


class Notifier:
    def __init__(self, bot: Bot, cfg):
        self.bot = bot
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    async def send_text(self, text: str, chat_id: str | None = None) -> object | None:
        target = chat_id or self.cfg.chat_id
        try:
            return await self.bot.send_message(chat_id=target, text=text, parse_mode="HTML")
        except TelegramError as exc:
            log.error("Telegram send failed to %s: %s", target, exc)
            return None
        except Exception as exc:  # network hiccups etc.
            log.error("Telegram send unexpected error: %s", exc)
            return None

    async def send_photo(self, photo: Path, caption: str) -> object | None:
        try:
            with open(photo, "rb") as fh:
                return await self.bot.send_photo(chat_id=self.cfg.chat_id, photo=fh, caption=caption, parse_mode="HTML")
        except TelegramError as exc:
            log.error("Telegram photo send failed: %s", exc)
            return None
        except Exception as exc:
            log.error("Telegram photo send unexpected error: %s", exc)
            return None

    async def edit_text(self, message: object, text: str) -> None:
        try:
            await self.bot.edit_message_text(
                chat_id=message.chat_id, message_id=message.message_id, text=text, parse_mode="HTML"
            )
        except TelegramError as exc:
            log.debug("Telegram edit failed (message may be too old): %s", exc)
        except Exception as exc:
            log.debug("Telegram edit unexpected error: %s", exc)

    # ------------------------------------------------------------------ #
    def build_signal_text(self, sig: dict, blackout_note: str = "") -> str:
        direction = sig["direction"]
        emoji = EMOJI.get(direction, "⚪")
        lines = [
            f"<b>{emoji} GOLD {'LONG' if direction == 'long' else 'SHORT'}</b> — XAU/USDT · {sig['timeframe']}",
            "━━━━━━━━━━━━━━━━━━",
            f"💰 Entry: <code>{format_price(sig['entry'])}</code>",
            f"🛑 SL: <code>{format_price(sig['sl'])}</code> ({sig['sl_atr_mult']}×ATR)",
            f"🎯 TP: <code>{format_price(sig['tp'])}</code> ({sig['tp_atr_mult']}×ATR) · RR 1:{sig['rr']:.1f}",
            f"🔥 Confidence: <b>{sig['score']}/100</b>",
            f"🧩 Setup: {sig['strategy_names']}",
        ]
        if sig.get("reasons"):
            lines.append("")
            lines.append("📌 Reasons:")
            for r in sig["reasons"][:6]:
                lines.append(f"• {html.escape(r)}")
        if blackout_note:
            lines.append("")
            lines.append(f"⏸ {blackout_note}")
        lines.append("")
        lines.append(f"⏰ {sig['time_str']} UTC · v{sig.get('version', '1.0.0')}")
        return "\n".join(lines)

    async def send_signal(self, sig: dict, chart_path: Path | None) -> object | None:
        """Post a signal (text + optional chart). Returns the text message (for AI edits)."""
        text = self.build_signal_text(sig)
        if chart_path and chart_path.exists():
            caption = text
            if len(caption) > 1000:  # Telegram caption limit is 1024
                caption = caption[:950] + "…"
            msg = await self.send_photo(chart_path, caption)
            if msg is None:
                msg = await self.send_text(text)
            return msg
        return await self.send_text(text)

    async def notify_error(self, text: str) -> None:
        if not self.cfg.error_notifications:
            return
        target = self.cfg.admin_chat_id or self.cfg.chat_id
        try:
            await self.bot.send_message(chat_id=target, text=f"⚠️ <b>Bot alert</b>\n{html.escape(text)}", parse_mode="HTML")
        except Exception as exc:
            log.error("Could not send error notification: %s", exc)
