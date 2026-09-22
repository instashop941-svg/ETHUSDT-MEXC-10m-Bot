# BTC + ETH 10m 6→15 PRE2MIN + WIN Bot

Signal-only Telegram bot for MEXC Futures using BTCUSDT and ETHUSDT.

## Logic
- Build closed 10m candles from 5m candles.
- Start candle is the last candle of its same-color run.
- #6 must be the opposite color of Start.
- #7, #8, #9 must match #6.
- About 2 minutes before #9 closes, if the forming #9 still matches #6, send the pre-signal warning.
- No final signal is sent at #9.
- #10, #11, #12, #13, #14, #15 must ALL match the Start candle color.
- Only after #15 closes is the setup confirmed and the final WIN message sent.
- Direction: GREEN Start = LONG; RED Start = SHORT.

## Railway variables
- TELEGRAM_BOT_TOKEN
- CHAT_ID (default/fallback TELEGRAM_CHAT_ID)
- SCAN_SECONDS=30
- LEVERAGE=30
- PRE_MIN_SECONDS=90
- PRE_MAX_SECONDS=150

No automatic trading. Public MEXC market data only.
