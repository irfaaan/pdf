"""Regression tests for Telegram HTML — any static string sent with
parse_mode=HTML must only contain Telegram-supported tags, otherwise
Telegram rejects the whole message ("Can't parse entities").

This guards against the `<module>` / `<param>` placeholder bug that broke
/help and /start.

Run:  python -m unittest tests.test_telegram_html -v
"""
from __future__ import annotations

import re
import unittest

from bot.commands import HELP_TEXT

ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "tg-spoiler",
}

TAG_RE = re.compile(r"</?([a-zA-Z0-9-]+)[^>]*>")


class TestTelegramHTML(unittest.TestCase):
    def test_help_text_has_no_bare_placeholders(self):
        self.assertNotIn("<module>", HELP_TEXT)
        self.assertNotIn("<param>", HELP_TEXT)
        self.assertNotIn("<value>", HELP_TEXT)
        self.assertNotIn("<minutes>", HELP_TEXT)
        self.assertNotIn("<name>", HELP_TEXT)
        self.assertIn("&lt;module&gt;", HELP_TEXT)
        self.assertIn("&lt;param&gt;", HELP_TEXT)
        self.assertIn("&lt;minutes&gt;", HELP_TEXT)
        self.assertIn("&lt;name&gt;", HELP_TEXT)

    def test_help_text_tags_all_allowed(self):
        for match in TAG_RE.findall(HELP_TEXT):
            self.assertIn(
                match.lower(),
                ALLOWED_TAGS,
                f"tag <{match}> is not supported by Telegram HTML",
            )

    def test_no_unclosed_or_unbalanced_tags(self):
        # Simple balance check for the tags we emit
        for tag in ("b", "code", "i", "u", "s", "pre"):
            opens = len(re.findall(rf"<{tag}\b[^>]*>", HELP_TEXT))
            closes = len(re.findall(rf"</{tag}>", HELP_TEXT))
            self.assertEqual(opens, closes, f"<{tag}> open/close mismatch in HELP_TEXT")

    def test_status_and_signal_templates_valid(self):
        """The templates used by cmd_status / notifier only use allowed tags."""
        from bot.notifier import Notifier

        sample = Notifier.build_signal_text(
            None,
            {
                "direction": "long",
                "timeframe": "5m",
                "entry": 3365.45,
                "sl": 3362.2,
                "tp": 3373.6,
                "sl_atr_mult": 1.5,
                "tp_atr_mult": 2.5,
                "rr": 1.7,
                "score": 82,
                "strategy_names": "EMA Pullback (strong)",
                "reasons": ["Uptrend above EMA50, pullback reclaimed EMA21", "Volume above average"],
                "time_str": "2026-08-21 10:50",
                "version": "1.0.0",
            },
        )
        for match in TAG_RE.findall(sample):
            self.assertIn(match.lower(), ALLOWED_TAGS, f"tag <{match}> not allowed in signal text")

    def test_help_text_renderable_plain(self):
        """After unescaping, /help reads correctly (no leftover entities)."""
        import html as h

        plain = h.unescape(re.sub(r"<[^>]+>", "", HELP_TEXT))
        self.assertNotIn("&lt;", plain)
        self.assertNotIn("&amp;", plain)
        self.assertIn("/enable <module>", plain)


if __name__ == "__main__":
    unittest.main()
