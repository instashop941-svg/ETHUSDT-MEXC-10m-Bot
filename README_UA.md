# BTCUSDT + ETHUSDT MEXC 10m Telegram Bot v4

REST-only bot for MEXC ETH_USDT Futures.

- Polls public MEXC 1m candles every 15 seconds.
- Builds closed 10m candles locally.
- Uses the agreed signal rule: last candle of a green/red run, then 6 subsequent candles; opposite color on candle 6 triggers LONG/SHORT.
- Candle #6 is the trigger; candles #7-#8 must remain the trigger color; candles #9-#15 decide WIN/LOSS. WIN occurs on any #9-#15 candle matching Start color; LOSS is sent only after #15 closes if none match.
- **On startup it seeds history without sending old historical signals.**
- Telegram messages use plain UTF-8 text without emoji, avoiding mojibake such as `â`.

Railway variables remain unchanged:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MEXC_SYMBOL`, `POLL_SECONDS`, `STATE_FILE`.


Оновлення: у групі рядок "Trigger: candle 6" приховано; у приватному чаті він залишається.
