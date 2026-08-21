"""Optional AI vetting of setups via OpenRouter (non-blocking).

When a setup forms and AI_ENABLED=true, the signal message is posted
immediately and this module asynchronously asks the configured model to
vet the trade.  The verdict is appended to the already-sent Telegram
message.  Any failure (bad key, bad model, timeout) is logged and the
signal stands on its own — AI is an enhancement, never a blocker.

Only the model's *final verdict line* is ever posted to Telegram.
Chain-of-thought / reasoning output (``<thinking>…``, ``reasoning``
fields, etc.) is stripped and logged at DEBUG level — it never reaches
the channel.
"""
from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

log = logging.getLogger("goldbot.ai")

SYSTEM_PROMPT = (
    "You are a professional XAU/USDT gold scalper. You are given a technical setup. "
    "Reply with EXACTLY ONE LINE, nothing else: CONFIRM, REJECT or NEUTRAL, "
    "followed by '|' and a one-sentence reason (max 140 chars). "
    "Be conservative: only CONFIRM when the setup is clean, confluence is high "
    "and you see no immediate counter-signal. "
    "Output ONLY that single line — no thinking, no reasoning, no chain of thought, "
    "no preamble, no code fences, no markdown."
)

MAX_VERDICT_LEN = 200

# Reasoning/thinking wrappers some models wrap around their answer.
_REASONING_BLOCKS = [
    re.compile(r"<\s*thinking\s*>.*?<\s*/\s*thinking\s*>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\s*reasoning\s*>.*?<\s*/\s*reasoning\s*>", re.DOTALL | re.IGNORECASE),
    re.compile(r"\[\s*thinking\s*\].*?\[\s*/\s*thinking\s*\]", re.DOTALL | re.IGNORECASE),
    re.compile(r"\[\s*reasoning\s*\].*?\[\s*/\s*reasoning\s*\]", re.DOTALL | re.IGNORECASE),
]

_VERDICT_WORDS = ("CONFIRM", "REJECT", "NEUTRAL")
_VERDICT_RE = re.compile(r"\b(CONFIRM|REJECT|NEUTRAL)\b", re.IGNORECASE)


def clean_verdict(raw: str | None, max_len: int = MAX_VERDICT_LEN) -> str | None:
    """Extract the exact one-line verdict from a model reply.

    - Strips ``<thinking>…`` / ``[reasoning]…`` blocks and ``` code-fence
      markers.
    - Returns the first line that contains CONFIRM / REJECT / NEUTRAL,
      normalised to ``KEYWORD | reason``.
    - Returns None when no verdict keyword is present, so reasoning-only
      replies are never posted to Telegram.
    """
    if not raw:
        return None
    text = str(raw).replace("```", "")
    for pattern in _REASONING_BLOCKS:
        text = pattern.sub("", text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    for line in lines:
        match = _VERDICT_RE.search(line)
        if match:
            kw = match.group(1).upper()
            rest = line[match.end():].lstrip(":|-—.,; ").strip()
            if rest:
                return f"{kw} | {rest}"[:max_len]
            return kw
    # No verdict keyword — do not post anything
    return None


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
        """Returns the exact verdict line (e.g. 'CONFIRM | reason') or None."""
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
            "max_tokens": 120,
            "temperature": 0.1,
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
            message = data["choices"][0]["message"]

            # Reasoning models return chain-of-thought in `message.reasoning`.
            # Log it for debugging, never post it.
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            if reasoning:
                log.debug("AI reasoning (not posted): %.400s", str(reasoning))

            content = message.get("content") or ""
            verdict = clean_verdict(content)
            if verdict is None:
                log.warning("AI returned no usable verdict: %.200s", content[:200])
            return verdict
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
