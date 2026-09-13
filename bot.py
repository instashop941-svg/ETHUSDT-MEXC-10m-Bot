import os
import json
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

MEXC_BASE = "https://api.mexc.com"
SYMBOL = os.getenv("MEXC_SYMBOL", "ETH_USDT")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
LOG_EVERY_SECONDS = int(os.getenv("LOG_EVERY_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("ethusdt-bot")


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float

    @property
    def color(self):
        if self.close > self.open:
            return "GREEN"
        if self.close < self.open:
            return "RED"
        return "DOJI"


def empty_state():
    return {
        "last_closed_ts": None,
        "last_start_ts": None,
        "last_start_color": None,
        "pending": [],
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return empty_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("pending", [])
        state.setdefault("last_start_ts", None)
        state.setdefault("last_start_color", None)
        state.setdefault("last_closed_ts", None)
        return state
    except Exception:
        log.exception("Cannot read state; starting fresh")
        return empty_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def mexc_get_1m(limit=300):
    """Fetch public MEXC Futures 1-minute candles."""
    url = f"{MEXC_BASE}/api/v1/contract/kline/{SYMBOL}"
    params = {"interval": "Min1", "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    payload = r.json()

    if not payload.get("success"):
        raise RuntimeError(f"MEXC error: {payload}")

    d = payload.get("data", {})
    times = d.get("time", [])
    if not times:
        raise RuntimeError(f"MEXC returned no candle data: {payload}")

    rows = []
    for i, ts in enumerate(times):
        rows.append({
            "ts": int(ts),
            "open": float(d["open"][i]),
            "high": float(d["high"][i]),
            "low": float(d["low"][i]),
            "close": float(d["close"][i]),
        })
    rows.sort(key=lambda x: x["ts"])
    return rows


def aggregate_10m(rows):
    """Build closed UTC 10-minute candles from 1-minute Futures candles."""
    buckets = {}
    for x in rows:
        bucket = (x["ts"] // 600) * 600
        buckets.setdefault(bucket, []).append(x)

    result = []
    now = int(time.time())
    for bucket, items in sorted(buckets.items()):
        if bucket + 600 > now:
            continue  # current 10m candle is still forming
        items.sort(key=lambda x: x["ts"])
        if len(items) < 9:
            continue  # protect against incomplete API history
        result.append(Candle(
            ts=bucket,
            open=items[0]["open"],
            high=max(x["high"] for x in items),
            low=min(x["low"] for x in items),
            close=items[-1]["close"],
        ))
    return result


def tg_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram is not configured: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        result = r.json()
        if not result.get("ok", False):
            raise RuntimeError(f"Telegram API error: {result}")
        log.info("Telegram message sent successfully")
    except Exception:
        log.exception("Telegram send failed")
        raise


def fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def color_emoji(color):
    return "ð¢" if color == "GREEN" else "ð´" if color == "RED" else "âª"


def process_new_candle(candles, idx, state):
    """
    Signal rule:
      - LAST green/red candle in a same-color run = START.
      - 6 candles after START: opposite color => signal.
      - green START + 6th red => LONG.
      - red START + 6th green => SHORT.
      - LONG wins if any green appears in candles 7..13.
      - SHORT wins if any red appears in candles 7..13.
      - Otherwise LOSS after candle 13.
      - Doji is neither green nor red.
    """
    if idx < 1:
        return

    c = candles[idx]
    prev = candles[idx - 1]

    # Color change means prev is the last candle of the previous run.
    if (
        prev.color in ("GREEN", "RED")
        and c.color in ("GREEN", "RED")
        and c.color != prev.color
    ):
        state["last_start_ts"] = prev.ts
        state["last_start_color"] = prev.color
        log.info(
            "New START candidate: %s %s (%s)",
            fmt_time(prev.ts),
            prev.color,
            "last candle of run",
        )

    start_ts = state.get("last_start_ts")
    start_color = state.get("last_start_color")
    if start_ts is None or start_color not in ("GREEN", "RED"):
        return

    start_index = next(
        (j for j in range(idx, -1, -1) if candles[j].ts == start_ts),
        None,
    )
    if start_index is None:
        return

    offset = idx - start_index

    # 6 candles after START = signal candle.
    if offset == 6:
        expected_trigger = "RED" if start_color == "GREEN" else "GREEN"
        log.info(
            "Checking signal: start=%s %s, 6th=%s %s, expected=%s",
            fmt_time(start_ts),
            start_color,
            fmt_time(c.ts),
            c.color,
            expected_trigger,
        )

        if c.color == expected_trigger:
            direction = "LONG" if start_color == "GREEN" else "SHORT"
            pending = state.setdefault("pending", [])
            if not any(p["trigger_ts"] == c.ts for p in pending):
                pending.append({
                    "direction": direction,
                    "start_ts": start_ts,
                    "trigger_ts": c.ts,
                    "signal_ts": c.ts,
                })
                tg_send(
                    f"ð {SYMBOL} â {direction}\n"
                    f"10m signal\n"
                    f"{color_emoji(start_color)} Start: {fmt_time(start_ts)}\n"
                    f"6-ÑÐ°: {color_emoji(c.color)} {fmt_time(c.ts)}\n"
                    f"ÐÐ¾Ð½ÑÑÐ¾Ð»Ñ WIN/LOSS: ÑÐ²ÑÑÐºÐ¸ 7â13"
                )
                log.info("SIGNAL %s at %s", direction, fmt_time(c.ts))

    # Check candles 7..13 for all pending signals.
    pending = state.setdefault("pending", [])
    still_pending = []
    for p in pending:
        rel = (c.ts - p["trigger_ts"]) // 600  # 1 => candle #7, 7 => #13
        if 1 <= rel <= 7:
            win_color = "GREEN" if p["direction"] == "LONG" else "RED"
            if c.color == win_color:
                tg_send(
                    f"â WIN â {SYMBOL} {p['direction']}\n"
                    f"Ð¡Ð¸Ð³Ð½Ð°Ð»: {fmt_time(p['signal_ts'])}\n"
                    f"ÐÑÐ´ÑÐ²ÐµÑÐ´Ð¶ÐµÐ½Ð½Ñ: ÑÐ²ÑÑÐºÐ° #{rel + 6}, "
                    f"{color_emoji(c.color)} {fmt_time(c.ts)}"
                )
                log.info(
                    "WIN %s trigger=%s",
                    p["direction"],
                    fmt_time(p["trigger_ts"]),
                )
                continue

            if rel == 7:
                tg_send(
                    f"â LOSS â {SYMBOL} {p['direction']}\n"
                    f"Ð¡Ð¸Ð³Ð½Ð°Ð»: {fmt_time(p['signal_ts'])}\n"
                    f"Ð£ ÑÐ²ÑÑÐºÐ°Ñ 7â13 Ð½Ðµ Ð±ÑÐ»Ð¾ Ð¿Ð¾ÑÑÑÐ±Ð½Ð¾Ð³Ð¾ ÐºÐ¾Ð»ÑÐ¾ÑÑ."
                )
                log.info(
                    "LOSS %s trigger=%s",
                    p["direction"],
                    fmt_time(p["trigger_ts"]),
                )
                continue

        if rel > 7:
            continue
        still_pending.append(p)

    state["pending"] = still_pending


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram is NOT configured. Check Railway Variables.")
        raise SystemExit(1)

    state = load_state()
    log.info("Started %s 10m signal bot", SYMBOL)
    log.info(
        "Config: poll=%ss, log_every=%ss, chat_id_configured=%s, token_configured=%s",
        POLL_SECONDS,
        LOG_EVERY_SECONDS,
        bool(TELEGRAM_CHAT_ID),
        bool(TELEGRAM_TOKEN),
    )

    last_heartbeat = 0

    while True:
        try:
            rows = mexc_get_1m()
            candles = aggregate_10m(rows)
            now = time.time()

            if now - last_heartbeat >= LOG_EVERY_SECONDS:
                latest = candles[-1] if candles else None
                if latest:
                    log.info(
                        "HEARTBEAT OK | MEXC 1m=%d | closed 10m=%d | latest=%s %s | O=%.4f C=%.4f | pending=%d",
                        len(rows),
                        len(candles),
                        fmt_time(latest.ts),
                        latest.color,
                        latest.open,
                        latest.close,
                        len(state.get("pending", [])),
                    )
                else:
                    log.warning(
                        "HEARTBEAT WARNING | MEXC 1m=%d | no closed 10m candles",
                        len(rows),
                    )
                last_heartbeat = now

            if not candles:
                time.sleep(POLL_SECONDS)
                continue

            last_ts = state.get("last_closed_ts")
            if last_ts is None:
                # First launch intentionally does not send historical signals.
                state["last_closed_ts"] = candles[-1].ts
                save_state(state)
                log.info(
                    "Initialized at %s (%s). Waiting for new 10m candles.",
                    fmt_time(candles[-1].ts),
                    candles[-1].color,
                )
            else:
                new = [c for c in candles if c.ts > last_ts]
                if new:
                    log.info("Processing %d new closed 10m candle(s)", len(new))

                for c in new:
                    idx = candles.index(c)
                    log.info(
                        "10m candle: %s %s | O=%.4f H=%.4f L=%.4f C=%.4f",
                        fmt_time(c.ts),
                        c.color,
                        c.open,
                        c.high,
                        c.low,
                        c.close,
                    )
                    process_new_candle(candles, idx, state)
                    state["last_closed_ts"] = c.ts

                if new:
                    save_state(state)

        except Exception:
            log.exception("Main loop error")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
