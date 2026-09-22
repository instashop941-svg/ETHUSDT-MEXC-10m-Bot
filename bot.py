import os
import time
from datetime import datetime, timezone

import ccxt
import requests


# ============================================================
# CONFIG
# ============================================================

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
]

DISPLAY_TIMEFRAME = "10m"
SOURCE_TIMEFRAME = "5m"

SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))
HISTORY_5M = int(os.getenv("HISTORY_5M", "240"))

# PRE-SIGNAL:
# approximately 2 minutes before candle #8 closes
PRE_MIN_SECONDS = int(os.getenv("PRE_MIN_SECONDS", "90"))
PRE_MAX_SECONDS = int(os.getenv("PRE_MAX_SECONDS", "150"))

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

# NEW BOT CHAT ID
CHAT_ID = os.getenv(
    "CHAT_ID",
    "-1003695570431"
).strip()


# ============================================================
# MEXC
# ============================================================

exchange = ccxt.mexc({
    "enableRateLimit": True,
    "timeout": 15000,
    "options": {
        "defaultType": "swap"
    },
})


# ============================================================
# STATE
# ============================================================

sent_keys = set()
warning_keys = set()
last_error = {}


# ============================================================
# TIME
# ============================================================

def now_ms():
    return int(time.time() * 1000)


def utc_text(ms):
    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


# ============================================================
# CANDLE COLOR
# ============================================================

def color(candle):
    open_price = float(candle[1])
    close_price = float(candle[4])

    if close_price > open_price:
        return "GREEN"

    if close_price < open_price:
        return "RED"

    return "DOJI"


# ============================================================
# BUILD CLOSED 10m FROM 5m
# ============================================================

def build_10m(candles5):
    """
    Build fully closed UTC-aligned 10m candles from 5m.

    00 + 05
    10 + 15
    20 + 25
    30 + 35
    40 + 45
    50 + 55
    """

    by_ts = {
        int(c[0]): c
        for c in candles5
    }

    out = []

    five = 5 * 60 * 1000
    now = now_ms()

    for ts, a in sorted(by_ts.items()):

        minute = (ts // 60000) % 60

        if minute % 10 != 0:
            continue

        b = by_ts.get(ts + five)

        if b is None:
            continue

        # Both 5m candles must be fully closed.
        if ts + 2 * five > now:
            continue

        out.append([
            ts,
            float(a[1]),
            max(
                float(a[2]),
                float(b[2])
            ),
            min(
                float(a[3]),
                float(b[3])
            ),
            float(b[4]),
            float(a[5]) + float(b[5]),
        ])

    return out


# ============================================================
# CURRENT 10m CANDLE
# ============================================================

def build_current_10m(raw5, live_price):
    """
    Build currently forming 10m candle.

    Used only for PRE-SIGNAL on candle #8.
    """

    if not raw5:
        return None

    by_ts = {
        int(c[0]): c
        for c in raw5
    }

    now = now_ms()

    five = 5 * 60 * 1000
    ten = 10 * 60 * 1000

    bucket = (now // ten) * ten

    a = by_ts.get(bucket)

    if a is None:
        return None

    b = by_ts.get(bucket + five)

    # We need the second 5m candle to exist.
    if b is None:
        return None

    open_price = float(a[1])

    high_price = max(
        float(a[2]),
        float(b[2]),
        float(live_price)
    )

    low_price = min(
        float(a[3]),
        float(b[3]),
        float(live_price)
    )

    volume = (
        float(a[5]) +
        float(b[5])
    )

    return [
        bucket,
        open_price,
        high_price,
        low_price,
        float(live_price),
        volume,
    ]


# ============================================================
# FETCH MEXC
# ============================================================

def fetch_raw_5m(symbol):
    return exchange.fetch_ohlcv(
        symbol,
        SOURCE_TIMEFRAME,
        limit=max(
            HISTORY_5M,
            240
        )
    )


def fetch_live_price(symbol):
    ticker = exchange.fetch_ticker(symbol)

    last = ticker.get("last")

    if last is None:
        raise RuntimeError(
            "ticker last price unavailable"
        )

    return float(last)


# ============================================================
# PRE-SIGNAL
# ============================================================

def pre_signal(
    candles_completed,
    current_10m
):
    """
    NEW BOT PRE-SIGNAL LOGIC

    #1 = Start

    #6, #7, #8 must be opposite
    to the color of #1.

    PRE-SIGNAL happens while #8
    is currently forming.

    Approximately 90-150 seconds
    before #8 closes.
    """

    # Need completed #1-#7.
    if (
        current_10m is None
        or len(candles_completed) < 7
    ):
        return None

    # Last 7 completed candles:
    #
    #1 #2 #3 #4 #5 #6 #7

    seq7 = candles_completed[-7:]

    start = seq7[0]
    c6 = seq7[5]
    c7 = seq7[6]

    # Current forming candle = #8
    c8 = current_10m

    start_color = color(start)

    if start_color not in (
        "GREEN",
        "RED"
    ):
        return None

    # ========================================================
    # START MUST BE LAST OF SAME-COLOR RUN
    # ========================================================

    if len(candles_completed) >= 8:

        previous = candles_completed[-8]

        if color(previous) == start_color:
            return None

    opposite = (
        "RED"
        if start_color == "GREEN"
        else "GREEN"
    )

    # ========================================================
    # #6 #7 #8 OPPOSITE
    # ========================================================

    if not (
        color(c6) == opposite
        and color(c7) == opposite
        and color(c8) == opposite
    ):
        return None

    # ========================================================
    # #8 CLOSE TIME
    # ========================================================

    close_ms = (
        int(c8[0])
        + 10 * 60 * 1000
    )

    remaining = (
        close_ms - now_ms()
    ) / 1000.0

    # Approximately 2 minutes before #8 closes.
    if not (
        PRE_MIN_SECONDS
        <= remaining
        <= PRE_MAX_SECONDS
    ):
        return None

    return {
        "side": (
            "LONG"
            if start_color == "GREEN"
            else "SHORT"
        ),

        "start_color": start_color,

        "c6": c6,
        "c7": c7,
        "c8": c8,

        "close_ms": close_ms,
    }


# ============================================================
# FINAL SIGNAL
# ============================================================

def find_signal(candles):
    """
    FINAL LOGIC

    #1 = Start

    #6 #7 #8 must ALL be opposite
    to #1.

    #9 #10 #11 #12 #13 #14 #15

    At least ONE must match
    the color of #1.

    ANY MATCH = WIN

    NONE = LOSS

    Final check ONLY after #15
    has fully closed.
    """

    if len(candles) < 15:
        return None

    # Latest 15 fully closed candles.
    i = len(candles) - 15

    c1 = candles[i]
    c6 = candles[i + 5]
    c7 = candles[i + 6]
    c8 = candles[i + 7]

    c9 = candles[i + 8]
    c10 = candles[i + 9]
    c11 = candles[i + 10]
    c12 = candles[i + 11]
    c13 = candles[i + 12]
    c14 = candles[i + 13]
    c15 = candles[i + 14]

    start_color = color(c1)

    if start_color not in (
        "GREEN",
        "RED"
    ):
        return None

    # ========================================================
    # START MUST BE LAST OF SAME-COLOR RUN
    # ========================================================

    if i + 1 < len(candles):

        next_candle = candles[i + 1]

        if color(next_candle) == start_color:
            return None

    opposite = (
        "RED"
        if start_color == "GREEN"
        else "GREEN"
    )

    # ========================================================
    # #6 #7 #8 ALL OPPOSITE
    # ========================================================

    if not (
        color(c6) == opposite
        and color(c7) == opposite
        and color(c8) == opposite
    ):
        return None

    # ========================================================
    # #9 - #15
    # AT LEAST ONE = START COLOR
    # ========================================================

    candles_9_15 = [
        c9,
        c10,
        c11,
        c12,
        c13,
        c14,
        c15,
    ]

    if not any(
        color(c) == start_color
        for c in candles_9_15
    ):
        # LOSS
        # No Telegram signal.
        return None

    # ========================================================
    # DIRECTION
    # ========================================================

    side = (
        "LONG"
        if start_color == "GREEN"
        else "SHORT"
    )

    return {
        "side": side,

        "start": c1,
        "start_color": start_color,

        "c6": c6,
        "c7": c7,
        "c8": c8,

        "c9": c9,
        "c10": c10,
        "c11": c11,
        "c12": c12,
        "c13": c13,
        "c14": c14,
        "c15": c15,
    }


# ============================================================
# TELEGRAM
# ============================================================

def telegram(text):

    if (
        not TELEGRAM_BOT_TOKEN
        or not CHAT_ID
    ):
        print(
            "[TELEGRAM] not configured",
            flush=True
        )
        return False

    try:

        response = requests.post(
            (
                "https://api.telegram.org/"
                f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            ),

            json={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },

            timeout=15,
        )

        if not response.ok:

            print(
                "[TELEGRAM ERROR] "
                f"{response.status_code}: "
                f"{response.text}",
                flush=True
            )

        return response.ok

    except Exception as e:

        print(
            f"[TELEGRAM ERROR] {e}",
            flush=True
        )

        return False


# ============================================================
# WARNING MESSAGE
# ============================================================

def warning_message(symbol):

    coin = symbol.split("/")[0]

    return (
        "Всі готові?\n\n"
        "Скоро дам СИГНАЛ!\n\n"
        f"{coin}USDT Futures\n\n"
        "Timeframe: 10m\n\n"
        "⚠️ Сигнал буде тільки "
        "після закриття 8-ї свічки."
    )


# ============================================================
# FINAL MESSAGE
# ============================================================

def signal_message(
    symbol,
    signal
):
    """
    Final message:
    - NO Leverage
    - NO @vasylpavliv
    - YES t.me/vasylpavliv
    """

    coin = symbol.split("/")[0]

    if signal["side"] == "LONG":
        title = "🟢 LONG"
    else:
        title = "🔴 SHORT"

    # Entry = close of #15.
    entry = float(
        signal["c15"][4]
    )

    return (
        f"{title}\n\n"
        f"{coin}USDT Futures\n\n"
        "Timeframe: 10m\n\n"
        f"Entry: {entry}\n\n"
        "Трейдер Василь Павлів\n\n"
        "t.me/vasylpavliv"
    )


# ============================================================
# PROCESS
# ============================================================

def process(symbol):

    try:

        raw = fetch_raw_5m(symbol)

        live_price = fetch_live_price(
            symbol
        )

        completed = build_10m(raw)

        current = build_current_10m(
            raw,
            live_price
        )

        if len(completed) < 15:

            raise RuntimeError(
                "not enough completed "
                f"10m candles: {len(completed)}"
            )

        # ====================================================
        # PRE-SIGNAL
        # ====================================================

        warning = pre_signal(
            completed,
            current
        )

        if warning:

            key = (
                symbol,
                int(warning["c8"][0]),
                "PRE",
            )

            if key not in warning_keys:

                text = warning_message(
                    symbol
                )

                if telegram(text):

                    warning_keys.add(
                        key
                    )

                print(
                    "\n=== PRE-SIGNAL #8 ===\n"
                    + text
                    + "\n=====================\n",
                    flush=True
                )

        # ====================================================
        # FINAL CHECK
        # ====================================================

        signal = find_signal(
            completed
        )

        if not signal:
            return

        # Entry = close of #15.
        entry = float(
            signal["c15"][4]
        )

        key = (
            symbol,
            signal["side"],
            int(signal["c15"][0]),
            "SIGNAL",
        )

        if key in sent_keys:
            return

        text = signal_message(
            symbol,
            signal
        )

        if telegram(text):

            sent_keys.add(
                key
            )

        print(
            "\n=== FINAL SIGNAL ===\n"
            + text
            + "\n====================\n",
            flush=True
        )

    except Exception as e:

        msg = str(e)

        if (
            last_error.get(symbol)
            != msg
        ):

            print(
                f"[FETCH ERROR] "
                f"{symbol}: {msg}",
                flush=True
            )

            last_error[symbol] = msg


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=== NEW BTC + ETH 10m "
        "6-8 / 9-15 ANY WIN BOT "
        "STARTING ===",
        flush=True
    )

    print(
        "This is a SEPARATE bot.",
        flush=True
    )

    print(
        "Imports OK",
        flush=True
    )

    print(
        f"Symbols: {', '.join(SYMBOLS)}",
        flush=True
    )

    print(
        "Timeframe: 10m "
        "(built from 5m candles)",
        flush=True
    )

    print(
        "Rule: #1 Start; "
        "#6-#8 opposite Start; "
        "ANY #9-#15 Start color = WIN",
        flush=True
    )

    print(
        f"Pre-signal: "
        f"{PRE_MIN_SECONDS}-"
        f"{PRE_MAX_SECONDS}s "
        "before #8 close",
        flush=True
    )

    print(
        "Final confirmation: "
        "only after candle #15 closes",
        flush=True
    )

    print(
        "If no #9-#15 candle "
        "matches Start color = LOSS",
        flush=True
    )

    print(
        f"Chat ID: {CHAT_ID}",
        flush=True
    )

    print(
        "Connecting to MEXC...",
        flush=True
    )

    exchange.load_markets()

    print(
        "MEXC connected. "
        f"Markets loaded: "
        f"{len(exchange.markets)}",
        flush=True
    )

    print(
        "=== NEW BTC + ETH 10m "
        "6-8 / 9-15 ANY WIN BOT "
        "RUNNING ===",
        flush=True
    )

    cycle = 0

    while True:

        started = time.time()

        for symbol in SYMBOLS:

            process(symbol)

        cycle += 1

        if cycle % 10 == 0:

            print(
                "[HEARTBEAT] "
                "NEW BTC+ETH bot alive | "
                f"scan={SCAN_SECONDS}s",
                flush=True
            )

        elapsed = (
            time.time()
            - started
        )

        sleep_for = max(
            1,
            SCAN_SECONDS - elapsed
        )

        print(
            f"[CYCLE] completed in "
            f"{elapsed:.1f}s | "
            f"sleep {sleep_for:.1f}s",
            flush=True
        )

        time.sleep(
            sleep_for
        )


if __name__ == "__main__":
    main()
