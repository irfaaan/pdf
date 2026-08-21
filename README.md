# Gold Scalper Bot — XAU/USDT (MEXC → Telegram)

A fully professional, async Python scalping-signal bot for **gold (XAU/USDT)**
on **MEXC**. It scans `1m / 5m / 15m` candles 24/7 with six confluence-based
scalping strategies, and the instant a **setup forms** it posts a rich
notification (entry / stop / target / confidence / reasons / chart) to your
Telegram channel. It also tracks every signal and reports win rate, P&L in R
and profit factor — plus a built-in **backtester** to tune parameters for the
best results.

```
┌──────────┐   poll 15s   ┌──────────────┐   setup forms   ┌──────────────┐
│   MEXC   │ ───────────► │  Engine      │ ──────────────► │   Telegram   │
│ XAU/USDT │  OHLCV/ATR   │  6 strategies│  msg + chart    │ @your_channel│
└──────────┘              └──────────────┘                 └──────────────┘
                                │
                                ▼
                    ┌──────────────────────┐
                    │ Tracking + backtest  │  win rate, R, profit factor
                    └──────────────────────┘
```

---

## ⚡ Quick start

```bash
# 1. install dependencies (Python 3.10+)
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. configure
cp .env.example .env               # then paste your TELEGRAM_TOKEN / CHANNEL_ID
nano .env

# 3. verify everything (config + Telegram + MEXC connectivity)
python run.py --selftest

# 4. run
python run.py                      # or: nohup python run.py &   /   docker-compose up -d
```

> **Before signals can be posted**: open your channel, add your bot as an
> **administrator** (Chat settings → Administrators → Add → your bot). Without
> this, Telegram refuses channel posts and `/test` will tell you so.

### Docker (recommended for a VPS)

```bash
docker-compose up -d --build
docker-compose logs -f
```

### systemd (no Docker)

See `deploy/gold-bot.service` — copy it to `/etc/systemd/system/`, fix the
paths, then `systemctl enable --now gold-bot`.

---

## 📋 Telegram commands

Send these to the bot in a private chat (or anywhere it can reply):

| Command | What it does |
|---|---|
| `/start` `/help` | Command reference |
| `/status` | Bot health: exchange latency, last candle per timeframe, toggles, uptime |
| `/price` | Current XAU/USDT price |
| `/chart` | Chart of the latest candles (default `15m`) |
| `/signals` | Last 5 signals (use `/signals 10`) |
| `/stats` | Tracked performance: win rate, total R, profit factor, per-strategy |
| `/test` | Posts a test message + chart to your channel |
| `/blackout` | News-blackout status |
| `/lastsignal` | Reprint the last emitted setup |
| `/enable gold` `/disable gold` | Pause / resume signal scanning live |
| `/enable charts` `/enable ai` `/enable broadcast` `/enable tracking` `/enable errors` `/enable news` | Live module toggles |
| `/set min_signal_score 70` | Live parameter change (see below) |
| `/cooldown 15` | Min minutes between same-direction signals |
| `/strategy ema_pullback on` | Live strategy toggle |
| `/strategies` | List strategies + state |
| `/reset` | Wipe tracked-signal history |

If `ADMIN_CHAT_ID` is set in `.env`, the mutating commands
(`/enable`, `/disable`, `/set`, `/cooldown`, `/strategy`, `/reset`) only work
from that chat id.

### Live-tunable parameters (`/set <param> <value>`)

| Param | Default | Meaning |
|---|---|---|
| `poll_interval_seconds` | 15 | Exchange polling frequency (3–300) |
| `signal_cooldown_minutes` | 10 | Min pause between same-direction signals per timeframe |
| `min_signal_score` | 65 | Confidence gate (10–100) — higher = fewer, stronger setups |
| `min_confluence_strategies` | 2 | How many strategies must agree (1–6) |
| `sl_atr_mult` | 1.5 | Stop-loss distance in ATRs |
| `tp_atr_mult` | 2.5 | Take-profit distance in ATRs |
| `vwap_dev_atr` | 1.8 | VWAP-reversion trigger distance in ATRs |

---

## 🧠 Strategies (all evaluated on **closed** candles only — no repainting)

| Strategy | Idea | Best on |
|---|---|---|
| **EMA Pullback** | Trend above EMA50; pullback into EMA21 with EMA9 cross / rejection candle + RSI confirmation | 5m, 15m |
| **VWAP Reversion** | Fade over-extensions (>1.8×ATR from session VWAP) with RSI extreme + reversal candle | 1m, 5m |
| **Break & Retest** | Donchian breakout on 1.5× volume, then retest of the broken level | 5m, 15m |
| **Order Block** | Supply/demand zones from strong reversal candles; retest with rejection | 5m, 15m |
| **MACD Momentum** | Fresh MACD cross + building histogram + trend filter + volume | 1m, 5m |
| **RSI Divergence** | Regular divergence at extremes with reversal candle (highest quality, rarest) | 1m, 5m |

Every setup requires **confluence**: by default at least **2 strategies** must
agree on the same direction, and the combined confidence must reach
**65/100**. That filter is what keeps the signal quality high — see the
backtest section for tuning.

### Safety filters (on by default)
- **News blackout** — suppresses notifications during high-impact US data
  (CPI/NFP 08:30 ET, FOMC 14:00 ET, both DST variants; editable via
  `NEWS_BLACKOUT_WINDOWS` in `.env`).
- **Cooldown + dedup** — one chance per candle per direction; no spam.
- **Weekend filter** — optional (`WEEKEND_FILTER=true`).

---

## 📊 Backtester — tune for the best results

The backtester runs the **exact same strategy code** as the live bot over real
historical MEXC candles, so results carry over to live.

```bash
python -m bot.backtest --tf 5m --days 30 --chart
python -m bot.backtest --tf 1m --days 7 --strategy vwap_reversion,rsi_divergence --chart
```

Output: setups found, trades, win rate, **total P&L in R**, profit factor,
max drawdown, and a per-strategy breakdown. An equity-curve PNG is saved to
`data/charts/`.

**How to use it to improve results:**
1. Baseline on `5m`, 30 days: `python -m bot.backtest --tf 5m --days 30 --chart`
2. Check per-strategy numbers — disable losers via `.env` strategy toggles.
3. Raise `MIN_SIGNAL_SCORE` / `MIN_CONFLUENCE_STRATEGIES` if win rate < 50 %.
4. Test `SL_ATR_MULT` / `TP_ATR_MULT` combinations (e.g. 1.5 / 3.0).
5. Re-run, keep the config with the best profit factor and drawdown.

---

## 📁 Project layout

```
├── run.py                  # entry point (python run.py)
├── bot/
│   ├── config.py           # .env parsing + validation
│   ├── engine.py           # scan loop, confluence, filters, emit
│   ├── strategies.py       # 6 scalping strategies + merging
│   ├── indicators.py       # EMA/RSI/MACD/ATR/BB/Stoch/VWAP/Donchian…
│   ├── exchange_client.py  # ccxt async MEXC wrapper (retry/health/fallback)
│   ├── notifier.py         # Telegram message/chart posting
│   ├── commands.py         # 17 Telegram commands
│   ├── ai_advisor.py       # optional OpenRouter vetting of setups
│   ├── tracking.py         # win/loss tracking -> /stats
│   ├── charting.py         # candlestick charts for notifications
│   ├── backtest.py         # python -m bot.backtest
│   └── main.py             # --selftest / --smoke / --once / live
├── tests/                  # offline unit + e2e tests (no network needed)
├── deploy/gold-bot.service # systemd unit
├── Dockerfile / docker-compose.yml
└── data/                   # logs, charts, signals.json (auto-created)
```

---

## 🛠 Troubleshooting

| Problem | Fix |
|---|---|
| `/test` fails to post to channel | Add the bot as **admin** of the channel |
| `TELEGRAM_TOKEN` invalid | Regenerate the token in @BotFather |
| `Symbol XAU/USDT:USDT not found` | Set `GOLD_SYMBOL=XAU/USDT` (spot) in `.env` |
| No signals at all | `/status` → check toggles & blackout; lower `MIN_SIGNAL_SCORE`/`MIN_CONFLUENCE_STRATEGIES`; gold needs volatility |
| API key errors | Public data needs **no keys**; delete or fix `MEXC_API_KEY` |
| Bot stops after reboot | Use `docker-compose up -d` or the systemd unit |
| Exchange unreachable | Bot auto-retries with backoff and alerts the channel after 5 min |

---

## 🔒 Security notes

- `.env` is git-ignored — never commit real credentials.
- The bot only uses **public market data**; your MEXC API keys are optional.
- If you shared this chat/logs containing your keys, consider rotating the
  Telegram token (BotFather → `/revoke`) and MEXC keys afterwards — cheap
  insurance.
- AI vetting sends setup summaries (price/levels only) to OpenRouter. Disable
  with `AI_ENABLED=false` if you prefer.

## ℹ️ Notes

- All times in notifications are **UTC**.
- XAU/USDT perpetual trades 24/7 on MEXC, but scalping is best during
  London/NY sessions (07:00–19:00 UTC) — expect fewer, cleaner setups then.
- The PDF you mentioned did not arrive with this task; the strategy
  parameters are industry-standard gold scalping defaults and fully
  configurable/tunable via `.env` and the backtester.
