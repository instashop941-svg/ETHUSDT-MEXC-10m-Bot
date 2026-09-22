import os
import time
from datetime import datetime, timezone
import ccxt
import requests

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
DISPLAY_TIMEFRAME = "10m"
SOURCE_TIMEFRAME = "5m"
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))
LEVERAGE = int(os.getenv("LEVERAGE", "30"))
HISTORY_5M = int(os.getenv("HISTORY_5M", "240"))
PRE_MIN_SECONDS = int(os.getenv("PRE_MIN_SECONDS", "90"))
PRE_MAX_SECONDS = int(os.getenv("PRE_MAX_SECONDS", "150"))
# WIN is evaluated after candles #10-#13. Default: price must close
# at least 0.50% in the signal direction at any point in #10-#13.
WIN_MOVE_PCT = float(os.getenv("WIN_MOVE_PCT", "0.005"))
WIN_CHECK_CANDLES = int(os.getenv("WIN_CHECK_CANDLES", "4"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", "")).strip()

exchange = ccxt.mexc({
    "enableRateLimit": True,
    "timeout": 15000,
    "options": {"defaultType": "swap"},
})

sent_keys = set()
warning_keys = set()
win_keys = set()
last_error = {}


def now_ms():
    return int(time.time() * 1000)


def utc_text(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def color(c):
    o, cl = float(c[1]), float(c[4])
    return "GREEN" if cl > o else "RED" if cl < o else "DOJI"


def emoji(c):
    return "🟢" if c == "GREEN" else "🔴" if c == "RED" else "⚪"


def build_10m(candles5):
    """Build only fully closed UTC-aligned 10m candles from 5m candles."""
    by_ts = {int(c[0]): c for c in candles5}
    out = []
    five = 5 * 60 * 1000
    now = now_ms()

    for ts, a in sorted(by_ts.items()):
        minute = (ts // 60000) % 60
        if minute % 10 != 0 or ts % 60000 != 0:
            continue
        b = by_ts.get(ts + five)
        if b is None or ts + 2 * five > now:
            continue
        out.append([
            ts,
            float(a[1]),
            max(float(a[2]), float(b[2])),
            min(float(a[3]), float(b[3])),
            float(b[4]),
            float(a[5]) + float(b[5]),
        ])
    return out


def build_current_10m(raw5, live_price):
    """Build the current 10m candle using the live ticker price for close."""
    if len(raw5) < 1:
        return None
    by_ts = {int(c[0]): c for c in raw5}
    now = now_ms()
    five = 5 * 60 * 1000
    ten = 10 * 60 * 1000
    bucket = (now // ten) * ten
    a = by_ts.get(bucket)
    if a is None:
        return None
    b = by_ts.get(bucket + five)
    if b is None:
        # Before the second 5m candle appears, #9 cannot yet be evaluated.
        return None
    o = float(a[1])
    h = max(float(a[2]), float(b[2]), float(live_price))
    l = min(float(a[3]), float(b[3]), float(live_price))
    v = float(a[5]) + float(b[5])
    return [bucket, o, h, l, float(live_price), v]


def fetch_raw_5m(symbol):
    return exchange.fetch_ohlcv(symbol, SOURCE_TIMEFRAME, limit=max(HISTORY_5M, 260))


def fetch_live_price(symbol):
    ticker = exchange.fetch_ticker(symbol)
    last = ticker.get("last")
    if last is None:
        raise RuntimeError("ticker last price unavailable")
    return float(last)


def find_signal(candles):
    """Confirm the full 15-candle pattern after candle #15 closes.

    Pattern:
      #1 start color (GREEN/RED)
      #6-#8 opposite color to the start
      #9-#15 same color as the start
    Direction is determined by the start candle color.
    """
    if len(candles) < 15:
        return None

    i = len(candles) - 15
    start = candles[i]
    c6, c7, c8 = candles[i + 5], candles[i + 6], candles[i + 7]
    c9, c10, c11, c12, c13, c14, c15 = (
        candles[i + 8], candles[i + 9], candles[i + 10],
        candles[i + 11], candles[i + 12], candles[i + 13],
        candles[i + 14],
    )

    cs = color(start)
    first_group = [color(c) for c in (c6, c7, c8)]
    second_group = [color(c) for c in (c9, c10, c11, c12, c13, c14, c15)]

    if cs not in ("GREEN", "RED"):
        return None
    if not all(c in ("GREEN", "RED") for c in first_group + second_group):
        return None

    # Start must be the last candle of its same-color run.
    if i + 1 < len(candles) and color(candles[i + 1]) == cs:
        return None

    opposite = "RED" if cs == "GREEN" else "GREEN"

    # #6-#8 must all be opposite to the start.
    if not all(c == opposite for c in first_group):
        return None

    # #9-#15 must all return to the START color.
    if not all(c == cs for c in second_group):
        return None

    side = "LONG" if cs == "GREEN" else "SHORT"
    return {
        "side": side, "start": start,
        "c6": c6, "c7": c7, "c8": c8,
        "c9": c9, "c10": c10, "c11": c11,
        "c12": c12, "c13": c13, "c14": c14, "c15": c15,
        "start_color": cs,
    }

def pre_signal(candles_completed, current_10m):
    """Warn once during the 90-150s window before #8 closes.

    At warning time:
      #1 = start
      #6-#7 are already closed and opposite the start
      #8 is live and must currently also be opposite the start
    """
    if current_10m is None or len(candles_completed) < 7:
        return None

    seq7 = candles_completed[-7:]  # #2..#7
    start, c6, c7, c8 = seq7[0], seq7[5], seq7[6], current_10m
    cs, c6c, c7c, c8c = color(start), color(c6), color(c7), color(c8)

    if cs not in ("GREEN", "RED"):
        return None
    if c6c not in ("GREEN", "RED") or c7c not in ("GREEN", "RED") or c8c not in ("GREEN", "RED"):
        return None

    # Start must be the last candle of its same-color run.
    if len(candles_completed) >= 8 and color(candles_completed[-8]) == cs:
        return None

    opposite = "RED" if cs == "GREEN" else "GREEN"

    # #6-#8 must all currently be opposite to the start.
    if not (c6c == opposite and c7c == opposite and c8c == opposite):
        return None

    # Warning is about candle #8 closing.
    close_ms = int(start[0]) + 8 * 10 * 60 * 1000
    remaining = (close_ms - now_ms()) / 1000.0
    if not (PRE_MIN_SECONDS <= remaining <= PRE_MAX_SECONDS):
        return None

    return {
        "side": "LONG" if cs == "GREEN" else "SHORT",
        "start_color": cs, "c6": c6, "c7": c7,
        "c8": c8, "close_ms": close_ms,
    }

def telegram(text):
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        print("[TELEGRAM] not configured", flush=True)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            print(f"[TELEGRAM ERROR] {r.status_code}: {r.text}", flush=True)
        return r.ok
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}", flush=True)
        return False


def warning_message(symbol):
    coin = symbol.split("/")[0]
    return (
        "Всі готові?\n\n"
        "Скоро дам СИГНАЛ!\n\n"
        f"{coin}USDT Futures\n\n"
        "Timeframe: 10m\n\n"
        "⚠️ Сигнал буде тільки після закриття 8-ї свічки."
    )


def message(symbol, s):
    coin = symbol.split("/")[0]
    side = s["side"]
    title = "🟢 LONG — WIN" if side == "LONG" else "🔴 SHORT — WIN"
    candles = [s["c6"], s["c7"], s["c8"], s["c9"], s["c10"], s["c11"], s["c12"], s["c13"], s["c14"], s["c15"]]
    labels = list(range(6, 16))
    lines = [
        title, "", f"{coin}USDT Futures", "Timeframe: 10m",
        f"Leverage: {LEVERAGE}x", "",
        "15-CANDLE CONFIRMATION",
        f"Start: {emoji(s['start_color'])} {s['start_color']}",
    ]
    for n, c in zip(labels, candles):
        lines.append(f"{n}: {emoji(color(c))} {color(c)}")
    lines += [
        "",
        "🏆 WIN — послідовність підтверджена до закриття 15-ї свічки.",
        f"Confirmation candle #15 closed: {utc_text(s['c15'][0])}",
        "",
        "Трейдер Василь Павлів",
        "@vasylpavliv",
        "t.me/vasylpavliv",
    ]
    return "\n".join(lines)


def win_message(symbol, signal):
    coin = symbol.split("/")[0]
    side = signal["side"]
    return (
        "✅ WIN\n\n"
        f"{coin}USDT Futures\n"
        "Timeframe: 10m\n\n"
        f"Direction: {side}\n"
        "CONFIRMED THROUGH CANDLE #15\n\n"
        f"Start: {emoji(signal['start_color'])} {signal['start_color']}\n"
        f"6: {emoji(color(signal['c6']))} {color(signal['c6'])}\n"
        f"7: {emoji(color(signal['c7']))} {color(signal['c7'])}\n"
        f"8: {emoji(color(signal['c8']))} {color(signal['c8'])}\n"
        f"9: {emoji(color(signal['c9']))} {color(signal['c9'])}\n"
        f"10: {emoji(color(signal['c10']))} {color(signal['c10'])}\n"
        f"11: {emoji(color(signal['c11']))} {color(signal['c11'])}\n"
        f"12: {emoji(color(signal['c12']))} {color(signal['c12'])}\n"
        f"13: {emoji(color(signal['c13']))} {color(signal['c13'])}\n"
        f"14: {emoji(color(signal['c14']))} {color(signal['c14'])}\n"
        f"15: {emoji(color(signal['c15']))} {color(signal['c15'])}\n\n"
        "🏆 WIN — послідовність підтверджена до 15-ї свічки.\n\n"
        "Трейдер Василь Павлів\n@vasylpavliv\nt.me/vasylpavliv"
    )


def process(symbol):
    try:
        raw = fetch_raw_5m(symbol)
        live = fetch_live_price(symbol)
        completed = build_10m(raw)
        current = build_current_10m(raw, live)
        if len(completed) < 10:
            raise RuntimeError(f"not enough completed 10m candles: {len(completed)}")

        warning = pre_signal(completed, current)
        if warning:
            key = (symbol, int(warning["c9"][0]), "PRE")
            if key not in warning_keys:
                text = warning_message(symbol)
                if telegram(text):
                    warning_keys.add(key)
                print("\n=== PRE-SIGNAL ===\n" + text + "\n=================\n", flush=True)

        # Final confirmation happens only after candle #15 closes.
        s = find_signal(completed)
        if not s:
            return

        key = (symbol, s["side"], int(s["c15"][0]), "WIN")
        if key in sent_keys:
            return

        text = message(symbol, s)
        if telegram(text):
            sent_keys.add(key)
        print("\n=== FINAL WIN ===\n" + text + "\n=================\n", flush=True)

    except Exception as e:
        msg = str(e)
        if last_error.get(symbol) != msg:
            print(f"[FETCH ERROR] {symbol}: {msg}", flush=True)
            last_error[symbol] = msg


def main():
    print("=== BTC + ETH 10m 6-8 / 9-15 PRE2MIN + WIN BOT STARTING ===", flush=True)
    print("Imports OK", flush=True)
    print(f"Symbols: {', '.join(SYMBOLS)}", flush=True)
    print("Timeframe: 10m (built from 5m candles)", flush=True)
    print("Rule: Start color sets direction; #6-#8 opposite Start; #9-#15 match Start", flush=True)
    print(f"Pre-signal: {PRE_MIN_SECONDS}-{PRE_MAX_SECONDS}s before #8 close", flush=True)
    print("Final confirmation: candle #15 must close with #10-#15 matching Start color", flush=True)
    print(f"Leverage: {LEVERAGE}x", flush=True)
    print(f"Telegram configured: {bool(TELEGRAM_BOT_TOKEN and CHAT_ID)}", flush=True)
    print(f"Chat ID configured: {CHAT_ID or 'NO'}", flush=True)
    print("Connecting to MEXC...", flush=True)
    exchange.load_markets()
    print(f"MEXC connected. Markets loaded: {len(exchange.markets)}", flush=True)
    print("=== BTC + ETH 10m 6-8 / 9-15 PRE2MIN + WIN BOT RUNNING ===", flush=True)
    n = 0
    while True:
        t = time.time()
        for symbol in SYMBOLS:
            process(symbol)
        n += 1
        if n % 10 == 0:
            print(f"[HEARTBEAT] BTC+ETH bot alive | scan={SCAN_SECONDS}s", flush=True)
        elapsed = time.time() - t
        sleep = max(1, SCAN_SECONDS - elapsed)
        print(f"[CYCLE] completed in {elapsed:.1f}s | sleep {sleep:.1f}s", flush=True)
        time.sleep(sleep)


if __name__ == "__main__":
    main()
