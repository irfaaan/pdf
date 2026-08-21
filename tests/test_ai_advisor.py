"""Offline tests for the AI verdict cleaner — guarantees reasoning/thinking
never reaches Telegram, only the exact verdict line does.

Run:  python -m unittest tests.test_ai_advisor -v
"""
from __future__ import annotations

import unittest

from bot.ai_advisor import clean_verdict


class TestCleanVerdict(unittest.TestCase):
    def test_plain_verdict(self):
        self.assertEqual(clean_verdict("CONFIRM | strong pullback at EMA21"), "CONFIRM | strong pullback at EMA21")

    def test_thinking_block_stripped(self):
        raw = "<thinking>Let me check the trend... the EMA21 held so this is likely a long.</thinking>\nCONFIRM | clean long continuation"
        self.assertEqual(clean_verdict(raw), "CONFIRM | clean long continuation")

    def test_reasoning_tags_stripped(self):
        raw = "[reasoning]Downtrend intact, RSI cooling.[/reasoning]\nREJECT | too close to VWAP"
        self.assertEqual(clean_verdict(raw), "REJECT | too close to VWAP")

    def test_reasoning_xml_block(self):
        raw = (
            "<reasoning>\nThe price is above VWAP by 2.3 ATR and RSI is 74, "
            "so a short reversion makes sense.\n</reasoning>\n"
            "CONFIRM | short reversion from over-extension"
        )
        self.assertEqual(clean_verdict(raw), "CONFIRM | short reversion from over-extension")

    def test_reasoning_field_content_only(self):
        # When the API puts CoT in `reasoning`, content holds only the answer —
        # but if the model also inlines it, we still strip it.
        raw = "The trade is fine.\nCONFIRM | momentum aligned with VWAP"
        self.assertEqual(clean_verdict(raw), "CONFIRM | momentum aligned with VWAP")

    def test_lowercase_keyword(self):
        self.assertEqual(clean_verdict("confirm | good setup"), "CONFIRM | good setup")

    def test_keyword_mid_line(self):
        self.assertEqual(clean_verdict("My verdict: NEUTRAL | waiting for breakout"), "NEUTRAL | waiting for breakout")

    def test_code_fence_stripped(self):
        raw = "```\nCONFIRM | clean\n```"
        self.assertEqual(clean_verdict(raw), "CONFIRM | clean")

    def test_reasoning_only_no_verdict(self):
        # No verdict keyword → nothing is posted (strict)
        raw = "I think the trend is up and the EMA held, so I'd lean long here."
        self.assertIsNone(clean_verdict(raw))

    def test_empty_returns_none(self):
        self.assertIsNone(clean_verdict(""))
        self.assertIsNone(clean_verdict(None))
        self.assertIsNone(clean_verdict("   \n  "))

    def test_length_capped(self):
        out = clean_verdict("CONFIRM | " + "x" * 500, max_len=50)
        self.assertLessEqual(len(out), 50)

    def test_bullet_or_dash_reason(self):
        raw = "REJECT:  spread too wide and news in 20 minutes"
        self.assertEqual(clean_verdict(raw), "REJECT | spread too wide and news in 20 minutes")

    def test_keyword_inside_reason_not_matched(self):
        # 'confirm' appears in the reason — must NOT override the NEUTRAL verdict
        raw = "NEUTRAL | waiting for the breakout to confirm"
        self.assertEqual(clean_verdict(raw), "NEUTRAL | waiting for the breakout to confirm")

    def test_punctuated_keyword(self):
        raw = "CONFIRM.  clean pullback on the 21 EMA"
        self.assertEqual(clean_verdict(raw), "CONFIRM | clean pullback on the 21 EMA")


if __name__ == "__main__":
    unittest.main()
