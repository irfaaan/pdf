"""Optional AI vetting of setups via OpenRouter (non-blocking).

When a setup forms and AI_ENABLED=true, the signal message is posted
immediately and this module asynchronously asks the configured model to
vet the trade.  The verdict is appended to the already-sent Telegram
message.  Any failure (bad key, bad model, timeout) is logged and the
signal stands on its own — AI is an enhancement, never a blocker.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger("goldbot.ai")

SYSTEM_PROMPT = (
    "You are a professional XAU/USDT gold scalper. You are given a technical setup. "
    "Reply with exactly one line: CONFIRM, REJECT or NEUTRAL, followed by '|' and a "
    "one-sentence reason (max 140 chars). Be conservative: only CONFIRM when the "
    "setup is clean, confluence is high and you see no immediate counter-signal."
)

MAX_VERDICT_LEN = 200


class AIAdvisor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = cfg.ai_enabled
        self._disabled_reason = ""

    # ------------------------------------------------------------------ #
    def build_prompt(self, signal_text: str, price: float, atr: float) -> str:
        return (
            f"Setup: {signal_text}\n"
            f"Current gold price: {price:.2f}, ATR: {atr:.2f}. "
            "Assess trend alignment, distance to VWAP, overbought/oversold risk and news risk. "
            "Verdict:"
        )

    async def vet(self, signal_text: str, price: float, atr: float) -> str | None:
        """Returns a verdict string like '✅ CONFIRM | reason' or None."""
        if not self.enabled:
            return None
        if not self.cfg.openrouter_keys:
            self._disable("no OpenRouter key configured")
            return None
        if not self.cfg.ai_model:
            self._disable("AI_MODEL not configured")
            return None

        prompt = self.build_prompt(signal_text, price, atr)
        headers = {
            "Authorization": f"Bearer {self.cfg.openrouter_keys[0]}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/irfaaan/pdf",
            "X-Title": "Gold Scalper Bot",
        }
        payload = {
            "model": self.cfg.ai_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 80,
            "temperature": 0.2,
        }
        url = f"{self.cfg.openrouter_base_url}/chat/completions"
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:200]
                        log.warning("AI vetting HTTP %s: %s", resp.status, body)
                        return None
                    data = await resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            return content[:MAX_VERDICT_LEN]
        except asyncio.TimeoutError:
            log.warning("AI vetting timed out (25s) — skipping")
            return None
        except Exception as exc:
            log.warning("AI vetting failed: %s", exc)
            return None

    def _disable(self, reason: str) -> None:
        if not self._disabled_reason:
            self._disabled_reason = reason
            self.enabled = False
            log.warning("AI vetting disabled: %s", reason)
